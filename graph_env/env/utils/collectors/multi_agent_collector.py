from collections import defaultdict
import copy
from typing import Any, cast
import warnings
import numpy as np
import time
from tianshou.data.types import RolloutBatchProtocol
import torch
from .collector import SingleAgentCollector, CollectStatsWithInfo, DictOfSequenceSummaryStats
from tianshou.data.buffer.vecbuf import VectorReplayBuffer
from tianshou.data import ReplayBuffer, ReplayBufferManager, CachedReplayBuffer, PrioritizedReplayBuffer, Batch, to_numpy, SequenceSummaryStats


class MultiAgentCollector(SingleAgentCollector):
    """
    MultiAgentCollector enables the policies of multiple agent to interact with the environment and save their
    experience in the replay buffer. The collector is responsible for managing the replay buffer and the
    experience of the agents. Similarly to Collector, MultiAgentCollector handles a buffer for every environment AND
    for every agent resulting in a VectorReplayBuffer with a buffer list of num_envs * num_agents. Each agent is
    assigned to specific buffer_id.

    As the iterations between the agents and the environment can have a stochastic order (e.g. Agent X could take three
    actions in a row, or Agent X and Y could alternate followed by a series of Y actions. X X X X Y X Y Y Y Y.)
    the collector, which basically takes an action on the current observation(s) and observes obs_next, will have two behaviors:
    - If obs and obs_next belong to the same buffer_id then the current experience can be added to the buffer
    (obs, term, trunc, done, info, obs_next).
    - If obs and obs_next do not belong to the same buffer_id, obs is saved to a temporary dictionary with the key
    being its buffer_id. The collector will wait the next obs_next with that buffer_id before adding obs to its buffer.
    """

    def __init__(self, agents_num, **kwargs):
        """
        Initialize the MultiAgentCollector.

        Args:
            agents_num (int): Number of agents in the environment.
            **kwargs: Additional keyword arguments passed to the parent class.
        """
        self.agents_num = agents_num
        self.temp_data = {}
        self.done_agents_per_env = {}
        super().__init__(**kwargs)

    def _assign_buffer(self, buffer: ReplayBuffer | None) -> None:
        """
        Assign the replay buffer to the collector.

        Args:
            buffer (ReplayBuffer | None): The replay buffer to be assigned. If None, a VectorReplayBuffer is created.
        """
        if buffer is None:
            buffer = VectorReplayBuffer(self.env_num * self.agents_num, self.env_num * self.agents_num)
        elif isinstance(buffer, ReplayBufferManager):
            assert buffer.buffer_num >= self.env_num * self.agents_num
            if isinstance(buffer, CachedReplayBuffer):
                assert buffer.cached_buffer_num >= self.env_num * self.agents_num
        else:
            assert buffer.maxsize > 0
            if self.env_num * self.agents_num > 1:
                if isinstance(buffer, ReplayBuffer):
                    buffer_type = "ReplayBuffer"
                    vector_type = "VectorReplayBuffer"
                if isinstance(buffer, PrioritizedReplayBuffer):
                    buffer_type = "PrioritizedReplayBuffer"
                    vector_type = "PrioritizedVectorReplayBuffer"
                raise TypeError(
                    f"Cannot use {buffer_type}(size={buffer.maxsize}, ...) to collect "
                    f"{self.env_num} envs,\n\tplease use {vector_type}(total_size="
                    f"{buffer.maxsize}, buffer_num={self.env_num}, ...) instead.",
                )
        self.buffer = buffer

    def reset(
        self,
        reset_buffer: bool = True,
        gym_reset_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """
        Reset the collector and optionally the replay buffer.

        Args:
            reset_buffer (bool): Whether to reset the replay buffer. Default is True.
            gym_reset_kwargs (dict[str, Any] | None): Extra keyword arguments to pass into the environment's reset function. Defaults to None.
        """
        super().reset(reset_buffer=reset_buffer, gym_reset_kwargs=gym_reset_kwargs)
        self.temp_data = {}
        self.done_agents_per_env = {i: [] for i in range(self.env_num)}

    def collect(
            self,
            n_step: int | None = None,
            n_episode: int | None = None,
            random: bool = False,
            render: bool = False,
            no_grad: bool = False,
            gym_render_kwargs: dict[str, Any] | None = None,
    ) -> CollectStatsWithInfo:
        """
        Collect a specified number of step or episode and saves them in the proper buffer based on the envs id and agent id.

        To ensure unbiased sampling result with n_episode option, this function will
        first collect ``n_episode - env_num`` episodes, then for the last ``env_num``
        episodes, they will be collected evenly from each envs.

        Args:
            n_step (int | None): How many steps you want to collect.
            n_episode (int | None): How many episodes you want to collect.
            random (bool): Whether to use random policy for collecting data. Default to False.
            render (bool): The sleep time between rendering consecutive frames. Default to None (no rendering).
            no_grad (bool): Whether to retain gradient in policy.forward(). Default to True (no gradient retaining).
            gym_render_kwargs (dict[str, Any] | None): Extra keyword arguments to pass into the environment's reset function. Defaults to None (extra keyword arguments).

        Returns:
            CollectStatsWithInfo: A dataclass object containing the collection statistics.

        .. note::
            One and only one collection number specification is permitted, either ``n_step`` or ``n_episode``.
        """
        assert not self.env.is_async, "MultiAgentCollector does not support async envs."
        if n_step is not None:
            assert n_episode is None, (
                f"Only one of n_step or n_episode is allowed in Collector."
                f"collect, got n_step={n_step}, n_episode={n_episode}."
            )
            assert n_step > 0
            if n_step % self.env_num != 0:
                warnings.warn(
                    f"n_step={n_step} is not a multiple of #envs ({self.env_num}), "
                    "which may cause extra transitions collected into the buffer.",
                )
            ready_env_ids = np.arange(self.env_num)
        elif n_episode is not None:
            assert n_episode > 0
            ready_env_ids = np.arange(min(self.env_num, n_episode))
            self.data = self.data[: min(self.env_num, n_episode)]
        else:
            raise TypeError(
                "Please specify at least one (either n_step or n_episode) "
                "in AsyncCollector.collect().",
            )

        start_time = time.time()

        step_count = 0
        episode_count = 0
        episode_returns: list[float] = []
        episode_lens: list[int] = []
        episode_start_indices: list[int] = []
        episode_info: list[dict[str, Any]] = []
        while True:
            assert len(self.data) == len(ready_env_ids), "Data should have one entry for each envs id."

            # restore the state (if any)
            last_state = self.data.policy.pop("hidden_state", None)

            # get the next action (one action from each environment)
            if random:
                try:
                    act_sample = [self._action_space[i].sample() for i in ready_env_ids]
                except TypeError:  # envpool's action space is not for per-envs
                    act_sample = [self._action_space.sample() for _ in ready_env_ids]
                act_sample = self.policy.map_action_inverse(act_sample)  # type: ignore
                self.data.update(act=act_sample)
            else:
                if no_grad:
                    with torch.no_grad():  # faster than retain_grad
                        # self.data.obs is extracted by the agent to get the result
                        result = self.policy(self.data, last_state)
                else:
                    result = self.policy(self.data, last_state)

                # update current batch of state / act / policy into self.data
                policy = result.get("policy", Batch())
                assert isinstance(policy, Batch), "The policy output should be a Batch."
                state = result.get("state", None)

                if state is not None:
                    policy.hidden_state = state  # save state into bufer

                # Add exploration noise if needed
                act = to_numpy(result.act)
                if self.exploration_noise:
                    act = self.policy.exploration_noise(act, self.data)

                # Update current batch DS
                self.data.update(policy=policy, act=act)

            # Get bounded and remapped actions from current batch
            action_remap = self.policy.map_action(self.data.act)

            # Step the environment(s)
            obs_next, rew, terminated, truncated, info = self.env.step(
                action_remap,
                ready_env_ids
            )
            done = np.logical_or(terminated, truncated)


            # Update current obs with next obs, reward, termination information
            # TODO: - in MA settings this should actually happen for each agent (when it's its turn again we can save it)
            self.data.update(
                obs_next=obs_next,
                rew=rew,
                terminated=terminated,
                truncated=truncated,
                done=done,
                info=info,
            )

            # Preprocess the data if needed
            if self.preprocess_fn:
                self.data.update(
                    self.preprocess_fn(
                        obs_next=self.data.obs_next,
                        rew=self.data.rew,
                        done=self.data.done,
                        info=self.data.info,
                        policy=self.data.policy,
                        env_id=ready_env_ids,
                        act=self.data.act,
                    )
                )

            if render:
                self.env.render()
                if render > 0 and not np.isclose(render, 0):
                    time.sleep(render)

            # Calculate buffer_ids based on current batch's env_ids and agent_ids
            # TODO: change the agent_ids ('agent_num' str to agent_id str)
            agent_id =  [int("".join(c for c in agent_num if c.isdigit())) for agent_num in self.data.obs.agent_id]
            buffer_ids = (self.data.info.env_id * self.agents_num) + agent_id

            next_agent_id = [int("".join(c for c in agent_num if c.isdigit())) for agent_num in self.data.obs_next.agent_id]
            next_buffer_ids = (self.data.info.env_id * self.agents_num) + next_agent_id

            experience_to_save = Batch()
            buffer_ids_to_save = []
            done_envs = []
            for (env_idx, env_id), (buffer_id, next_buffer_id) in zip(enumerate(self.data.info.env_id), zip(buffer_ids, next_buffer_ids)):
                 # Is the next obs in TMP? If so update the TMP exp info and save the experience
                if next_buffer_id in self.temp_data.keys():
                    # If that tuple is waiting for a new obs, save the reward
                    self.temp_data[next_buffer_id].rew = self.data.rew[[env_idx]]
                    self.temp_data[next_buffer_id].obs_next = self.data.obs_next[[env_idx]]
                    self.temp_data[next_buffer_id].terminated = self.data.terminated[[env_idx]]
                    self.temp_data[next_buffer_id].truncated = self.data.truncated[[env_idx]]
                    self.temp_data[next_buffer_id].done = self.data.done[[env_idx]]
                    self.temp_data[next_buffer_id].info = self.data.info[[env_idx]]
                    experience_to_save = Batch.cat([experience_to_save, self.temp_data[next_buffer_id]])
                    buffer_ids_to_save.append(next_buffer_id)
                    # Add data to experience to save
                    self.temp_data.pop(next_buffer_id)

                # If obs is followed by the same agent's obs, save the experience directly
                if buffer_id == next_buffer_id:
                    experience_to_save = Batch.cat([experience_to_save, self.data[[env_idx]]])
                    buffer_ids_to_save.append(buffer_id)
                    # TODO: double check if we need to pop the next buffer id (like in Collective Exp. Collector)

                if done[env_idx]:
                    self.done_agents_per_env[env_id].append(next_buffer_id)
                    if len(self.done_agents_per_env[env_id]) == self.agents_num or info[env_idx].get("explicit_reset", False):
                        done_envs.append((env_idx, env_id, len(experience_to_save)-1))

                # If save the current experience and wait for the next obs to save the proper reward and other info
                if buffer_id != next_buffer_id and not len(self.done_agents_per_env[env_id]) == self.agents_num and not buffer_id in self.done_agents_per_env[env_id]:
                    self.temp_data[buffer_id] = copy.deepcopy(self.data[[env_idx]])

            if len(buffer_ids_to_save) > 0:
                ptr, ep_rew, ep_len, ep_idx = self.buffer.add(experience_to_save, buffer_ids=buffer_ids_to_save)

                # Collect stats
                step_count += len(buffer_ids_to_save)  # How many envs have stepped all the agents

            episode_info.extend(info)

            if len(done_envs):
                env_ind_local = [env_idx for env_idx,_ ,_ in done_envs]
                env_ind_global = [env_id for _, env_id, _ in done_envs]
                env_ind_experience_to_save = [env_idx for _, _, env_idx in done_envs]
                episode_count += len(done_envs)
                episode_lens.extend(ep_len[env_ind_experience_to_save])
                episode_returns.extend(ep_rew[env_ind_experience_to_save])
                episode_start_indices.extend(ep_idx[env_ind_experience_to_save])

                for env_idx in env_ind_global:
                    self.done_agents_per_env[env_idx] = []

                # Now we copy obs_next to obs, but sincere might be finished episodes, we reset finished episodes first.
                self._reset_env_with_ids(env_ind_local, env_ind_global, gym_render_kwargs)
                for i in env_ind_local:
                    self._reset_state(i)

                # Remove finished envs from ready_env_ids to avoid bias in selecting envs.
                if n_episode:
                    surplus_env_num = len(ready_env_ids) - (n_episode - episode_count)
                    if surplus_env_num > 0:
                        mask = np.ones_like(ready_env_ids, dtype=bool)
                        mask[env_ind_local[:surplus_env_num]] = False
                        ready_env_ids = ready_env_ids[mask]
                        self.data = self.data[mask]

            # Update current obs to the next one and then continue
            self.data.obs = self.data.obs_next

            if (n_step and step_count >= n_step) or (n_episode and episode_count >= n_episode):
                break

        # generate statistics
        self.collect_step += step_count
        self.collect_episode += episode_count
        collect_time = max(time.time() - start_time, 1e-9)
        self.collect_time += collect_time

        # Add the custom statistics to the episode_info
        fused_logger_stats = defaultdict(list)
        for d in episode_info:
            logger_stats = d.pop('logger_stats', {})
            for key, value in logger_stats.items():
                fused_logger_stats[key].append(value)
        episode_info = fused_logger_stats

        if n_episode:
            data = Batch(
                obs={},
                act={},
                rew={},
                terminated={},
                truncated={},
                done={},
                obs_next={},
                info={},
                policy={},
            )
            self.data = cast(RolloutBatchProtocol, data)
            self.reset_env()

        return CollectStatsWithInfo(
            n_collected_episodes=episode_count,
            n_collected_steps=step_count,
            collect_time=collect_time,
            collect_speed=step_count / collect_time,
            returns=np.array(episode_returns),
            returns_stat=SequenceSummaryStats.from_sequence(episode_returns)
            if len(episode_returns) > 0
            else None,
            lens=np.array(episode_lens, int),
            lens_stat=SequenceSummaryStats.from_sequence(episode_lens)
            if len(episode_lens) > 0
            else None,
            info=DictOfSequenceSummaryStats.from_dict(episode_info)
        )
