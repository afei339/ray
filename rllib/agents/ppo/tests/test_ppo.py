import numpy as np
import unittest

import ray
import ray.rllib.agents.ppo as ppo
from ray.rllib.agents.ppo.ppo_tf_policy import postprocess_ppo_gae as \
    postprocess_ppo_gae_tf, ppo_surrogate_loss as ppo_surrogate_loss_tf
from ray.rllib.agents.ppo.ppo_torch_policy import postprocess_ppo_gae as \
    postprocess_ppo_gae_torch, ppo_surrogate_loss as ppo_surrogate_loss_torch
from ray.rllib.evaluation.postprocessing import Postprocessing
from ray.rllib.models.tf.tf_action_dist import Categorical
from ray.rllib.models.torch.torch_modelv2 import TorchModelV2
from ray.rllib.models.torch.torch_action_dist import TorchCategorical
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.framework import try_import_tf
from ray.rllib.utils.numpy import fc
from ray.rllib.utils.test_utils import check, framework_iterator

tf = try_import_tf()


class TestPPO(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init()

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()

    def test_ppo_compilation(self):
        """Test whether a PPOTrainer can be built with both frameworks."""
        config = ppo.DEFAULT_CONFIG.copy()
        config["num_workers"] = 0  # Run locally.
        num_iterations = 2

        for _ in framework_iterator(config):
            trainer = ppo.PPOTrainer(config=config, env="CartPole-v0")
            for i in range(num_iterations):
                trainer.train()

    def test_ppo_fake_multi_gpu_learning(self):
        """Test whether PPOTrainer can learn CartPole w/ faked multi-GPU."""
        config = ppo.DEFAULT_CONFIG.copy()
        # Fake GPU setup.
        config["num_gpus"] = 2
        config["_fake_gpus"] = True
        # Mimick tuned_example for PPO CartPole.
        config["num_workers"] = 1
        config["lr"] = 0.0003
        config["observation_filter"] = "MeanStdFilter"
        config["num_sgd_iter"] = 6
        config["vf_share_layers"] = True
        config["vf_loss_coeff"] = 0.01
        config["model"]["fcnet_hiddens"] = [32]
        config["model"]["fcnet_activation"] = "linear"

        trainer = ppo.PPOTrainer(config=config, env="CartPole-v0")
        num_iterations = 200
        learnt = False
        for i in range(num_iterations):
            results = trainer.train()
            if results["episode_reward_mean"] > 150:
                learnt = True
                break
            print(results)
        assert learnt, "PPO multi-GPU (with fake-GPUs) did not learn CartPole!"

    def test_ppo_exploration_setup(self):
        """Tests, whether PPO runs with different exploration setups."""
        config = ppo.DEFAULT_CONFIG.copy()
        config["num_workers"] = 0  # Run locally.
        config["env_config"] = {"is_slippery": False, "map_name": "4x4"}
        obs = np.array(0)

        # Test against all frameworks.
        for fw in framework_iterator(config):
            # Default Agent should be setup with StochasticSampling.
            trainer = ppo.PPOTrainer(config=config, env="FrozenLake-v0")
            # explore=False, always expect the same (deterministic) action.
            a_ = trainer.compute_action(
                obs,
                explore=False,
                prev_action=np.array(2),
                prev_reward=np.array(1.0))
            # Test whether this is really the argmax action over the logits.
            if fw != "tf":
                last_out = trainer.get_policy().model.last_output()
                check(a_, np.argmax(last_out.numpy(), 1)[0])
            for _ in range(50):
                a = trainer.compute_action(
                    obs,
                    explore=False,
                    prev_action=np.array(2),
                    prev_reward=np.array(1.0))
                check(a, a_)

            # With explore=True (default), expect stochastic actions.
            actions = []
            for _ in range(300):
                actions.append(
                    trainer.compute_action(
                        obs,
                        prev_action=np.array(2),
                        prev_reward=np.array(1.0)))
            check(np.mean(actions), 1.5, atol=0.2)

    def test_ppo_loss_function(self):
        """Tests the PPO loss function math."""
        config = ppo.DEFAULT_CONFIG.copy()
        config["num_workers"] = 0  # Run locally.
        config["gamma"] = 0.99
        config["model"]["fcnet_hiddens"] = [10]
        config["model"]["fcnet_activation"] = "linear"
        config["vf_share_layers"] = True

        # Fake CartPole episode of n time steps.
        train_batch = {
            SampleBatch.CUR_OBS: np.array(
                [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8],
                 [0.9, 1.0, 1.1, 1.2]],
                dtype=np.float32),
            SampleBatch.ACTIONS: np.array([0, 1, 1]),
            SampleBatch.PREV_ACTIONS: np.array([0, 1, 1]),
            SampleBatch.REWARDS: np.array([1.0, -1.0, .5], dtype=np.float32),
            SampleBatch.PREV_REWARDS: np.array(
                [1.0, -1.0, .5], dtype=np.float32),
            SampleBatch.DONES: np.array([False, False, True]),
            SampleBatch.VF_PREDS: np.array([0.5, 0.6, 0.7], dtype=np.float32),
            SampleBatch.ACTION_DIST_INPUTS: np.array(
                [[-2., 0.5], [-3., -0.3], [-0.1, 2.5]], dtype=np.float32),
            SampleBatch.ACTION_LOGP: np.array(
                [-0.5, -0.1, -0.2], dtype=np.float32),
        }

        for fw, sess in framework_iterator(
                config, frameworks=["eager", "tf", "torch"], session=True):
            trainer = ppo.PPOTrainer(config=config, env="CartPole-v0")
            policy = trainer.get_policy()

            # Post-process (calculate simple (non-GAE) advantages) and attach
            # to train_batch dict.
            # A = [0.99^2 * 0.5 + 0.99 * -1.0 + 1.0, 0.99 * 0.5 - 1.0, 0.5] =
            # [0.50005, -0.505, 0.5]
            if fw == "tf" or fw == "eager":
                train_batch = postprocess_ppo_gae_tf(policy, train_batch)
            else:
                train_batch = postprocess_ppo_gae_torch(policy, train_batch)
                train_batch = policy._lazy_tensor_dict(train_batch)

            # Check Advantage values.
            check(train_batch[Postprocessing.VALUE_TARGETS],
                  [0.50005, -0.505, 0.5])

            # Calculate actual PPO loss.
            if fw == "eager":
                ppo_surrogate_loss_tf(policy, policy.model, Categorical,
                                      train_batch)
            elif fw == "torch":
                ppo_surrogate_loss_torch(policy, policy.model,
                                         TorchCategorical, train_batch)

            vars = policy.model.variables() if fw != "torch" else \
                list(policy.model.parameters())
            if fw == "tf":
                vars = policy.get_session().run(vars)
            expected_shared_out = fc(train_batch[SampleBatch.CUR_OBS], vars[0],
                                     vars[1])
            expected_logits = fc(expected_shared_out, vars[2], vars[3])
            expected_value_outs = fc(expected_shared_out, vars[4], vars[5])

            kl, entropy, pg_loss, vf_loss, overall_loss = \
                self._ppo_loss_helper(
                    policy, policy.model,
                    Categorical if fw != "torch" else TorchCategorical,
                    train_batch,
                    expected_logits, expected_value_outs,
                    sess=sess
                )
            if sess:
                policy_sess = policy.get_session()
                k, e, pl, v, tl = policy_sess.run(
                    [
                        policy.loss_obj.mean_kl, policy.loss_obj.mean_entropy,
                        policy.loss_obj.mean_policy_loss,
                        policy.loss_obj.mean_vf_loss, policy.loss_obj.loss
                    ],
                    feed_dict=policy._get_loss_inputs_dict(
                        train_batch, shuffle=False))
                check(k, kl)
                check(e, entropy)
                check(pl, np.mean(-pg_loss))
                check(v, np.mean(vf_loss), decimals=4)
                check(tl, overall_loss, decimals=4)
            else:
                check(policy.loss_obj.mean_kl, kl)
                check(policy.loss_obj.mean_entropy, entropy)
                check(policy.loss_obj.mean_policy_loss, np.mean(-pg_loss))
                check(
                    policy.loss_obj.mean_vf_loss, np.mean(vf_loss), decimals=4)
                check(policy.loss_obj.loss, overall_loss, decimals=4)

    def _ppo_loss_helper(self,
                         policy,
                         model,
                         dist_class,
                         train_batch,
                         logits,
                         vf_outs,
                         sess=None):
        """
        Calculates the expected PPO loss (components) given Policy,
        Model, distribution, some batch, logits & vf outputs, using numpy.
        """
        # Calculate expected PPO loss results.
        dist = dist_class(logits, policy.model)
        dist_prev = dist_class(train_batch[SampleBatch.ACTION_DIST_INPUTS],
                               policy.model)
        expected_logp = dist.logp(train_batch[SampleBatch.ACTIONS])
        if isinstance(model, TorchModelV2):
            expected_rho = np.exp(expected_logp.detach().numpy() -
                                  train_batch.get(SampleBatch.ACTION_LOGP))
            # KL(prev vs current action dist)-loss component.
            kl = np.mean(dist_prev.kl(dist).detach().numpy())
            # Entropy-loss component.
            entropy = np.mean(dist.entropy().detach().numpy())
        else:
            if sess:
                expected_logp = sess.run(expected_logp)
            expected_rho = np.exp(expected_logp -
                                  train_batch[SampleBatch.ACTION_LOGP])
            # KL(prev vs current action dist)-loss component.
            kl = dist_prev.kl(dist)
            if sess:
                kl = sess.run(kl)
            kl = np.mean(kl)
            # Entropy-loss component.
            entropy = dist.entropy()
            if sess:
                entropy = sess.run(entropy)
            entropy = np.mean(entropy)

        # Policy loss component.
        pg_loss = np.minimum(
            train_batch.get(Postprocessing.ADVANTAGES) * expected_rho,
            train_batch.get(Postprocessing.ADVANTAGES) * np.clip(
                expected_rho, 1 - policy.config["clip_param"],
                1 + policy.config["clip_param"]))

        # Value function loss component.
        vf_loss1 = np.power(
            vf_outs - train_batch.get(Postprocessing.VALUE_TARGETS), 2.0)
        vf_clipped = train_batch.get(SampleBatch.VF_PREDS) + np.clip(
            vf_outs - train_batch.get(SampleBatch.VF_PREDS),
            -policy.config["vf_clip_param"], policy.config["vf_clip_param"])
        vf_loss2 = np.power(
            vf_clipped - train_batch.get(Postprocessing.VALUE_TARGETS), 2.0)
        vf_loss = np.maximum(vf_loss1, vf_loss2)

        # Overall loss.
        if sess:
            policy_sess = policy.get_session()
            kl_coeff, entropy_coeff = policy_sess.run(
                [policy.kl_coeff, policy.entropy_coeff])
        else:
            kl_coeff, entropy_coeff = policy.kl_coeff, policy.entropy_coeff
        overall_loss = np.mean(-pg_loss + kl_coeff * kl +
                               policy.config["vf_loss_coeff"] * vf_loss -
                               entropy_coeff * entropy)
        return kl, entropy, pg_loss, vf_loss, overall_loss


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main(["-v", __file__]))
