from functools import partial
from typing import Callable, Dict, List, NamedTuple, Tuple

import jax
import jax.numpy as jnp
import numpy as np
from chex import PRNGKey
from flax.training.train_state import TrainState
from omegaconf import DictConfig

from modules.agent.core import Agent
from modules.agent.utils import QTrainState
from modules.buffer import ReplayBuffer
from modules.hindsight import HerSampler
from modules.mpi_utils.mpi_utils import tree_all_reduce
from modules.mpi_utils.normalizer import Normalizer
from modules.networks import DeterministicActor, VectorCritic


class Batch(NamedTuple):
    observation: jax.Array
    achieved_goal: jax.Array
    goal: jax.Array
    action: jax.Array
    next_observation: jax.Array
    done: jax.Array
    reward: jax.Array


class DDPG(Agent):
    def __init__(
        self,
        rng_key: PRNGKey,
        env_samples: Dict,
        cfg: DictConfig,
        env_params: Dict,
        compute_rew: Callable,
    ):
        super().__init__(env_samples, cfg, env_params)
        self.rng_key, actor_key, critic_key, dropout_key = jax.random.split(rng_key, 4)
        self.actor_network = DeterministicActor(np.prod(env_params["action"]), cfg, env_params)
        self.critic_network = VectorCritic(cfg, env_params)
        self.setup(actor_key, critic_key, dropout_key)
        self.o_norm = Normalizer(size=self.env_params["obs"], default_clip_range=self.cfg.clip_range)
        self.g_norm = Normalizer(size=self.env_params["goal"], default_clip_range=self.cfg.clip_range)
        self.her_module = HerSampler(self.cfg, compute_rew)
        self.buffer = ReplayBuffer(
            env_params=self.env_params,
            buffer_size=self.cfg.buffer_size,
            sample_func=self.her_module.sample_her_transitions,
        )

    @staticmethod
    @jax.jit
    def explore(actor_state, inputs: List[jax.Array], rng_key):
        rng_key, subkey = jax.random.split(rng_key)
        action = actor_state.apply_fn(actor_state.params, *inputs)
        return (action + jax.random.normal(subkey, (action.shape[-1],))).clip(-1.0, 1.0), rng_key

    @staticmethod
    @jax.jit
    def exploit(actor_state, inputs: List[jax.Array]):
        return actor_state.apply_fn(actor_state.params, *inputs)

    def act(self, obs: np.ndarray, ag: np.ndarray, g: np.ndarray, with_noise: bool) -> np.ndarray:
        input_tensor = jax.device_put(self._preproc_inputs(obs, ag, g))
        if with_noise:
            action, self.rng_key = self.explore(self.actor_state, [input_tensor], self.rng_key)
        else:
            action = self.exploit(self.actor_state, [input_tensor])
        return jax.device_get(action)

    def get_normalizer_stats(self) -> Dict:
        return {
            "o_norm": {"mean": self.o_norm.mean, "std": self.o_norm.std},
            "g_norm": {"mean": self.g_norm.mean, "std": self.g_norm.std},
        }

    def _preproc_inputs(self, obs: np.ndarray, ag: np.ndarray, g: np.ndarray) -> np.ndarray:
        obs_norm = self.o_norm.normalize(obs)
        ag_norm = self.g_norm.normalize(ag)
        g_norm = self.g_norm.normalize(g)
        return np.concatenate([obs_norm, ag_norm, g_norm], axis=-1)

    def _update_normalizer(self, episode) -> None:
        mb_obs = np.concatenate(episode["obs"])
        mb_ag = np.concatenate(episode["ag"])
        mb_g = np.concatenate(episode["g"])
        mb_actions = np.concatenate(episode["action"])
        mb_next_obs = np.concatenate(episode["obs"][:, 1:, :])
        mb_next_ag = np.concatenate(episode["ag"][:, 1:, :])
        # get the number of normalization transitions
        num_transitions = mb_actions.shape[0]
        buffer_temp = {
            "obs": np.expand_dims(mb_obs, 0),
            "ag": np.expand_dims(mb_ag, 0),
            "g": np.expand_dims(mb_g, 0),
            "action": np.expand_dims(mb_actions, 0),
            "next_obs": np.expand_dims(mb_next_obs, 0),
            "next_ag": np.expand_dims(mb_next_ag, 0),
        }
        transitions = self.her_module.sample_her_transitions(buffer_temp, num_transitions)
        self.o_norm.update(transitions["obs"])
        self.o_norm.recompute_stats()

        if self.cfg.normalize_goal:
            self.g_norm.update(transitions["g"])
            self.g_norm.recompute_stats()

    def _update_networks(self, transitions: Dict[str, jax.Array]) -> Dict:
        batch = Batch(
            observation=self.o_norm.normalize(transitions["obs"]),
            achieved_goal=self.g_norm.normalize(transitions["ag"]),
            goal=self.g_norm.normalize(transitions["g"]),
            action=transitions["action"],
            next_observation=self.o_norm.normalize(transitions["next_obs"]),
            done=transitions["done"],
            reward=transitions["reward"],
        )
        (
            self.actor_state,
            self.critic_state,
            self.rng_key,
            metric_dict,
        ) = self.update(
            self.actor_state,
            self.critic_state,
            self.rng_key,
            batch,
        )
        return jax.device_get(metric_dict)

    @partial(jax.jit, static_argnums=(0,))
    def update(
        self,
        actor_state: TrainState,
        critic_state: QTrainState,
        rng_key: PRNGKey,
        batch: Batch,
    ) -> Tuple[TrainState, QTrainState, PRNGKey, Dict]:
        batch = batch._replace(
            observation=jnp.concatenate([batch.observation, batch.achieved_goal, batch.goal], axis=1)
        )
        batch = batch._replace(
            next_observation=jnp.concatenate([batch.next_observation, batch.achieved_goal, batch.goal], axis=1)
        )

        rng_key, dropout_key, dropout_target_key = jax.random.split(rng_key, 3)

        next_actions = actor_state.apply_fn(actor_state.params, batch.next_observation)

        next_q_values = critic_state.apply_fn(
            critic_state.target_params,
            batch.next_observation,
            next_actions,
            rngs={"dropout": dropout_target_key},
        )

        def critic_loss_fn(critic_params):
            min_next_q_values = jnp.min(next_q_values, axis=0)
            # shape is (batch_size, 1)
            if self.cfg.done_signal:
                target_q_values = jax.lax.stop_gradient(
                    batch.reward.reshape(-1, 1) + self.cfg.gamma(1 - batch.done.reshape(-1, 1)) * min_next_q_values
                )
            else:
                target_q_values = jax.lax.stop_gradient(
                    batch.reward.reshape(-1, 1) + self.cfg.gamma * min_next_q_values
                )

            # shape is (n_critics, batch_size, 1)
            current_q_values = critic_state.apply_fn(
                critic_params,
                batch.observation,
                batch.action,
                rngs={"dropout": dropout_key},
            )
            critic_loss = 0.5 * ((current_q_values - target_q_values) ** 2).mean()
            return critic_loss, {
                "q_value": current_q_values.mean(),
                "q_target": target_q_values.mean(),
                "critic_loss": critic_loss,
            }

        def update_critic(_critic_state):
            grads, aux_metrics = jax.grad(critic_loss_fn, has_aux=True)(_critic_state.params)
            global_grads = tree_all_reduce(grads)
            _critic_state = _critic_state.apply_gradients(grads=global_grads)
            _critic_state = _critic_state.soft_update(self.cfg.agent.critic.tau)
            return (_critic_state, aux_metrics)

        def actor_loss_fn(actor_params):
            actions = actor_state.apply_fn(actor_params, batch.observation)
            _critic_state, critic_metrics = update_critic(critic_state)

            qf_pi = critic_state.apply_fn(
                critic_state.params,
                batch.observation,
                actions,
                rngs={"dropout": dropout_key},
            )
            q_values = qf_pi.min(axis=0)
            actor_loss = -(q_values).mean()
            logs = {
                "actor_loss": actor_loss,
                **critic_metrics,
            }
            return actor_loss, (_critic_state, logs)

        grads, (critic_state, aux_metrics) = jax.grad(actor_loss_fn, has_aux=True)(actor_state.params)
        global_grads = tree_all_reduce(grads)
        actor_state = actor_state.apply_gradients(grads=global_grads)
        return actor_state, critic_state, rng_key, aux_metrics
