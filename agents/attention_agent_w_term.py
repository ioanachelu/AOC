import numpy as np
import tensorflow as tf
from tools.agent_utils import get_mode, update_target_graph_aux, update_target_graph_sf, \
  update_target_graph_option, discount, reward_discount, set_image, make_gif, set_image_plain
import os
from auxilary.policy_iteration import PolicyIteration
import matplotlib.patches as patches
import matplotlib.pylab as plt
import numpy as np
from collections import deque
import seaborn as sns
import matplotlib.pyplot as plt
from matplotlib import cm
sns.set()
import random
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from agents.eigenoc_agent_dynamic import EigenOCAgentDyn
import pickle
import copy
from threading import Barrier, Thread

FLAGS = tf.app.flags.FLAGS

"""This Agent is a specialization of the successor representation direction based agent with buffer SR matrix, but instead of choosing from discreate options that are grounded in the SR basis only by means of the pseudo-reward, it keeps a singly intra-option policy whose context is changed by means of the option given as embedding (the embedding being the direction given by the spectral decomposition of the SR matrix)"""
class AttentionWTermAgent(EigenOCAgentDyn):
  def __init__(self, sess, game, thread_id, global_step, global_episode, config, global_network, barrier):
    super(AttentionWTermAgent, self).__init__(sess, game, thread_id, global_step, global_episode, config, global_network, barrier)
    self.episode_mean_values_mix = []

  def init_episode(self):
    super(AttentionWTermAgent, self).init_episode()
    self.episode_mixed_reward = 0
    self.episode_intrinsic_reward = 0
    self.episode_values_mix = []
    self.episode_buffer_option = []
    self.episode_screens = []
    self.episode_directions = []
    self.episode_attention_weights = []
    self.reward_mix = 0
    self.reward_i = 0
    self.episode_length = 0
    self.episode_state_occupancy = np.zeros((self.nb_states))
    self.summaries_critic = self.summaries_option = self.summaries_term = self.summaries_direction = None
    self.R = self.R_mix = None

  def init_agent(self):
    super(AttentionWTermAgent, self).init_agent()
    self.clusters_folder = os.path.join(self.summary_path, "clusters")
    tf.gfile.MakeDirs(self.clusters_folder)

    self.policy_folder = os.path.join(self.summary_path, "policies_clusters")
    tf.gfile.MakeDirs(self.policy_folder)

    self.learning_progress_folder = os.path.join(self.summary_path, "learning_progress")
    tf.gfile.MakeDirs(self.learning_progress_folder)

    self.cluster_model_path = os.path.join(self.config.logdir, "cluster_models")
    tf.gfile.MakeDirs(self.cluster_model_path)

  """Starting point of the agent acting in the environment"""
  def play(self, coord, saver):
    self.saver = saver

    with self.sess.as_default(), self.sess.graph.as_default():
      self.init_agent()

      with coord.stop_on_exception():
        while not coord.should_stop():
          if (self.config.steps != -1 and \
                  (self.global_step_np > self.config.steps and self.name == "worker_0")) or \
              (self.global_episode_np > len(self.config.goal_locations) * self.config.move_goal_nb_of_ep and
                   self.name == "worker_0" and self.config.multi_task):
            coord.request_stop()
            return 0

          """update local network parameters from global network"""
          self.sync_threads()
          self.init_episode()

          """Reset the environment and get the initial state"""
          s = self.env.get_initial_state()
          s_screen = self.env.build_screen_for_state(s)
          """Choose an option"""
          self.direction_evaluation(s)
          """While the episode does not terminate"""
          while not self.done:
            """update local network parameters from global network"""
            self.sync_threads()

            """Choose an action from the current intra-option policy"""
            self.policy_evaluation(s)

            self.episode_state_occupancy[s] += 1
            s1_screen, self.reward, self.done, s1 = self.env.special_step(self.action, s)
            self.episode_reward += self.reward

            """Check if the option terminates at the next state"""
            self.direction_terminate(s1)

            """If the episode ended make the last state absorbing"""
            if self.done:
              s1, s1_screen = s, s_screen

            self.episode_buffer_sf.append([s])
            self.episode_screens.append(s_screen)

            self.compute_intrinsic_reward(s, s1)

            self.prev_option_direction = self.current_option_direction
            self.prev_query_direction = self.query_direction
            self.prev_attention_weights = self.attention_weights
            self.prev_query_content_match = self.query_content_match
            self.prev_q_s_o = self.q_s_o
            self.prev_q_mix_s_o = self.q_mix_s_o

            """If the option terminated or the option was primitive, sample another option"""
            if not self.done and self.term:
              self.direction_evaluation(s1)

            self.sf_prediction(s1)

            if self.global_step_np >= self.config.cold_start_sf_steps:
              self.option_prediction(s, s1)

            self.episode_mixed_reward += self.reward_mix
            self.episode_intrinsic_reward += self.reward_i

            if self.total_steps % self.config.step_summary_interval == 0 and self.name == 'worker_0':
              self.write_step_summary()

            s, s_screen = s1, s1_screen
            self.episode_length += 1
            self.total_steps += 1

            if self.name == "worker_0":
              self.sess.run(self.increment_global_step)
              self.global_step_np = self.global_step.eval()

          self.update_episode_stats()

          if self.name == "worker_0":
            self.sess.run(self.increment_global_episode)
            self.global_episode_np = self.global_episode.eval()

            if self.global_episode_np % self.config.checkpoint_interval == 0:
              self.save_model()
            if self.global_episode_np % self.config.summary_interval == 0:
              self.write_summaries()

            if self.global_episode_np % self.config.cluster_interval == 0:
                self.print_current_option_direction()

          """If it's time to change the task - move the goal, wait for all other threads to finish the current task"""
          if self.total_episodes % self.config.move_goal_nb_of_ep == 0 and \
                  self.total_episodes != 0:
            tf.logging.info(f"Moving GOAL....{self.total_episodes}")

            self.barrier.wait()
            self.goal_position = self.env.set_goal(self.total_episodes, self.config.move_goal_nb_of_ep)

          self.total_episodes += 1

  def compute_intrinsic_reward(self, s, s1):
    # reward_i = self.current_option_direction[s1] - self.current_option_direction[s]
    reward_i = self.cosine_similarity(self.current_option_direction, np.identity(self.nb_states)[s1] - np.identity(self.nb_states)[s])
    self.reward_i = reward_i
    self.reward_mix = self.config.alpha_r * reward_i + (1 - self.config.alpha_r) * self.reward

  """Check is the direction terminates at the next state"""
  def direction_terminate(self, s1):
    feed_dict = {self.local_network.observation: np.identity(self.nb_states)[s1:s1+1],
                 self.local_network.current_option_direction: [self.current_option_direction]}
    term = self.sess.run(self.local_network.termination, feed_dict=feed_dict)[0]
    self.term = term > np.random.uniform()
    # self.term = True

  def direction_evaluation(self, s):
    feed_dict = {self.local_network.observation: np.identity(self.nb_states)[s:s+1],
                 self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters(),}
    results = self.sess.run({
      "current_option_direction": self.local_network.current_option_direction,
      "query_direction": self.local_network.query_direction,
      "attention_weights": self.local_network.attention_weights,
      "query_content_match": self.local_network.query_content_match,
      "q_s_o": self.local_network.q_ext,
      "q_mix_s_o": self.local_network.q_mix
      }, feed_dict=feed_dict)
    self.current_option_direction = results["current_option_direction"][0]
    self.query_direction = results["query_direction"][0]
    self.attention_weights = results["attention_weights"][0]
    self.query_content_match = results["query_content_match"][0]
    self.q_s_o = results["q_s_o"][0]
    self.q_mix_s_o = results["q_mix_s_o"][0]


  """Sample an action from the current option's policy"""
  def policy_evaluation(self, s):
    feed_dict = {self.local_network.observation: np.identity(self.nb_states)[s:s+1],
                 self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters(),
                 self.local_network.attention_weights: [self.attention_weights],
                 self.local_network.current_option_direction: [self.current_option_direction]
                 }
    tensor_results = {
                   "sf": self.local_network.sf,
                   # "value_mix": self.local_network.v_mix,
                   "option_policy": self.local_network.option_policy}
    results = self.sess.run(tensor_results, feed_dict=feed_dict)

    sf = results["sf"][0]
    self.add_SF(sf)

    # self.value_mix = results["value_mix"][0]

    pi = results["option_policy"][0]

    # self.episode_values_mix.append(self.value_mix)

    """Sample an action"""
    self.action = np.random.choice(pi, p=pi)
    self.action = np.argmax(pi == self.action)

    if self.global_step_np < self.config.cold_start_sf_steps:
      self.action = np.random.choice(range(self.action_size))

    """Store information in buffers for stats in tensorboard"""
    self.episode_actions.append(self.action)

  """Do n-step prediction for the returns and update the option policies and critics"""
  def option_prediction(self, s, s1):
    """Adding to the transition buffer for doing n-step prediction on critics and policies"""
    self.episode_buffer_option.append(
      [s, self.action, self.reward, self.reward_mix, s1])
    self.episode_directions.append(self.prev_option_direction)
    self.episode_attention_weights.append(self.prev_attention_weights)

    if len(self.episode_buffer_option) >= self.config.max_update_freq or self.done or \
          self.term: # and len(self.episode_buffer_option) >= self.config.min_update_freq):
      """Get the bootstrap option-value functions for the next time step"""
      if self.done:
        bootstrap_V_mix = 0
        bootstrap_V_ext = 0
      else:
        feed_dict = {self.local_network.observation: np.identity(self.nb_states)[s1:s1+1],
                     self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters(),
                     self.local_network.current_option_direction: [self.current_option_direction],
                     self.local_network.attention_weights: [self.attention_weights]
                     }
        to_run = {"q_mix": self.local_network.q_mix,
                  # "v_mix": self.local_network.v_mix,
                  # "v_ext": self.local_network.v_ext,
                  "q_ext": self.local_network.q_ext}
        results = self.sess.run(to_run, feed_dict=feed_dict)
        # q_mix, v_mix, v_ext, q_ext = results["q_mix"][0], results["v_mix"][0], results["v_ext"][0], results["q_ext"][0]
        q_mix, q_ext = results["q_mix"][0], results["q_ext"][0]
        # bootstrap_V_mix = v_mix if self.term else q_mix
        bootstrap_V_mix = q_mix
        # bootstrap_V_ext = v_ext if self.term else q_ext
        bootstrap_V_ext = q_ext

      self.train_option(bootstrap_V_mix, bootstrap_V_ext, s1)
      self.episode_buffer_option = []
      self.episode_directions = []
      self.episode_attention_weights = []


  """Do n-step prediction for the successor representation latent and an update for the representation latent using 1-step next frame prediction"""
  def sf_prediction(self, s1):
    if len(self.episode_buffer_sf) == self.config.max_update_freq or self.done:
      """Get the successor features of the next state for which to bootstrap from"""
      feed_dict = {self.local_network.observation: [np.identity(self.nb_states)[s1]]}
      next_sf = self.sess.run(self.local_network.sf,
                         feed_dict=feed_dict)[0]
      bootstrap_sf = np.zeros_like(next_sf) if self.done else next_sf
      self.train_sf(bootstrap_sf)
      self.episode_buffer_sf = []
      self.episode_screens = []

  """Do one n-step update for training the agent's latent successor representation space and an update for the next frame prediction"""
  def train_sf(self, bootstrap_sf):
    rollout = np.array(self.episode_buffer_sf)
    observations = rollout[:, 0]
    fi = np.identity(self.nb_states)[observations]

    """Construct list of latent representations for the entire trajectory"""
    sf_plus = np.asarray(fi.tolist() + [bootstrap_sf])
    """Construct the targets for the next step successor representations for the entire trajectory"""
    discounted_sf = discount(sf_plus, self.config.discount)[:-1]

    feed_dict = {self.local_network.target_sf: np.stack(discounted_sf, axis=0),
                 self.local_network.observation_image: np.stack(self.episode_screens),
                 self.local_network.observation: np.identity(self.nb_states)[observations]}

    to_run = {"summary_sf": self.local_network.merged_summary_sf,
              "sf_loss": self.local_network.sf_loss
    }
    if self.name != "worker_0":
      to_run["apply_grads_sf"] = self.local_network.apply_grads_sf
    results = self.sess.run(to_run, feed_dict=feed_dict)
    self.summaries_sf = results["summary_sf"]

  def add_SF(self, sf):
    self.global_network.direction_clusters.cluster(sf)

  """Do n-step prediction on the critics and policies"""
  def train_option(self, bootstrap_value_mix, bootstrap_value_ext, s1):
    rollout = np.array(self.episode_buffer_option)
    observations = np.array(rollout[:, 0], dtype=np.int32)
    actions = rollout[:, 1]
    rewards = rollout[:, 2]
    rewards_mix = rollout[:, 3]
    option_directions = self.episode_directions
    attenton_weights = self.episode_attention_weights

    """Construct list of discounted returns using mixed reward signals for the entire n-step trajectory"""
    rewards_plus = np.asarray(rewards.tolist() + [bootstrap_value_ext])
    discounted_returns = reward_discount(rewards_plus, self.config.discount)[:-1]

    rewards_mix_plus = np.asarray(rewards_mix.tolist() + [bootstrap_value_mix])
    discounted_returns_mix = reward_discount(rewards_mix_plus, self.config.discount)[:-1]

    feed_dict = {self.local_network.target_return: discounted_returns,
                 self.local_network.target_mix_return: discounted_returns_mix,
                 self.local_network.observation: np.identity(self.nb_states)[observations],
                 self.local_network.actions_placeholder: actions,
                 self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters(),
                 self.local_network.current_option_direction: np.stack(option_directions, 0),
                 self.local_network.attention_weights: np.stack(attenton_weights, 0),
                 }

    to_run = {
             "summary_option": self.local_network.merged_summary_option,
             "summary_critic": self.local_network.merged_summary_critic,
             "q_ext": self.local_network.q_ext,
            }
    if self.name != "worker_0":
      to_run["apply_grads_option"] = self.local_network.apply_grads_option
      to_run["apply_grads_critic"] = self.local_network.apply_grads_critic

    """Do an update on the intra-option policies"""
    results = self.sess.run(to_run, feed_dict=feed_dict)
    self.summaries_option = results["summary_option"]
    self.summaries_critic = results["summary_critic"]
    q_ext = results["q_ext"]
    q_ext_next = np.asarray(q_ext.tolist()[1:] + [bootstrap_value_ext])

    feed_dict = {self.local_network.target_return: discounted_returns,
                 self.local_network.target_mix_return: discounted_returns_mix,
                 self.local_network.observation: np.identity(self.nb_states)[observations],
                 self.local_network.actions_placeholder: actions,
                 self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters(),
                 self.local_network.current_option_direction: np.stack(option_directions, 0),
                 self.local_network.attention_weights: np.stack(attenton_weights, 0),
                 self.local_network.target_v_ext: q_ext_next
                 }

    to_run = {
      "summary_term": self.local_network.merged_summary_term,
    }
    if self.name != "worker_0":
      to_run["apply_grads_term"] = self.local_network.apply_grads_term

    results = self.sess.run(to_run, feed_dict=feed_dict)
    self.summaries_term = results["summary_term"]

    next_observations = np.asarray(observations.tolist()[1:] + [s1])
    target_directions = np.identity(self.nb_states)[next_observations] - np.identity(self.nb_states)[observations]

    feed_dict = {self.local_network.target_return: discounted_returns,
                 self.local_network.observation: np.identity(self.nb_states)[observations],
                 self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters(),
                 self.local_network.target_direction: target_directions
                 }
    to_run = {"summary_direction": self.local_network.merged_summary_direction}
    if self.name != "worker_0":
      to_run["apply_grad_direction"] = self.local_network.apply_grads_direction

    """Do an update on the intra-option policies"""
    results = self.sess.run(to_run, feed_dict=feed_dict)
    self.summaries_direction = results["summary_direction"]

    """Store the bootstrap target returns at the end of the trajectory"""
    self.R_mix = discounted_returns_mix[-1]
    self.R = discounted_returns[-1]

  def write_step_summary(self):
    self.summary = tf.Summary()
    self.summary.value.add(tag='Step/Action', simple_value=self.action)
    self.summary.value.add(tag='Step/MixedReward', simple_value=self.reward_mix)
    self.summary.value.add(tag='Step/IntrinsicReward', simple_value=self.reward_i)
    self.summary.value.add(tag='Step/Reward', simple_value=self.reward)
    # self.summary.value.add(tag='Step/V_Mix', simple_value=self.value_mix)
    # self.summary.value.add(tag='Step/V_Ext', simple_value=self.value_ext)
    self.summary.value.add(tag='Step/Q_s_o', simple_value=self.q_s_o)
    self.summary.value.add(tag='Step/Q_mix_s_o', simple_value=self.q_mix_s_o)
    self.summary.value.add(tag='Step/Target_Return_Mix', simple_value=self.R_mix)
    self.summary.value.add(tag='Step/Target_Return', simple_value=self.R)
    self.summary.value.add(tag='Step/Term', simple_value=self.term)

    self.summary_writer.add_summary(self.summary, self.total_steps)
    self.summary_writer.flush()

  def update_episode_stats(self):
    if len(self.episode_values_mix) != 0:
      self.episode_mean_values_mix.append(np.mean(self.episode_values_mix))
    if len(self.episode_actions) != 0:
      self.episode_mean_actions.append(get_mode(self.episode_actions))

  def write_summaries(self):
    self.summary = tf.Summary()
    self.summary.value.add(tag='Perf/UndiscReturn', simple_value=float(self.episode_reward))
    self.summary.value.add(tag='Perf/UndiscMixedReturn', simple_value=float(self.episode_mixed_reward))
    self.summary.value.add(tag='Perf/UndiscIntrinsicReturn', simple_value=float(self.episode_intrinsic_reward))
    self.summary.value.add(tag='Perf/Length', simple_value=float(self.episode_length))

    for sum in [self.summaries_sf, self.summaries_term, self.summaries_critic, self.summaries_option, self.summaries_direction]:
      if sum is not None:
        self.summary_writer.add_summary(sum, self.global_episode_np)

    if len(self.episode_mean_values_mix) != 0:
      last_mean_value_mix = np.mean(self.episode_mean_values_mix[-self.config.step_summary_interval:])
      self.summary.value.add(tag='Perf/MixValue', simple_value=float(last_mean_value_mix))
    if len(self.episode_mean_actions) != 0:
      last_frequent_action = self.episode_mean_actions[-1]
      self.summary.value.add(tag='Perf/FreqActions', simple_value=last_frequent_action)

    self.summary_writer.add_summary(self.summary, self.global_episode_np)
    self.summary_writer.flush()

  """Plot plicies and value functions"""

  def plot_policy_and_value_function(self, eigenvectors):
    epsilon = 0.0001
    with self.sess.as_default(), self.sess.graph.as_default():
      self.env.define_network(self.local_network)
      self.env.define_session(self.sess)
      for i in range(len(eigenvectors)):
        """Do policy iteration"""
        discount = 0.9
        polIter = PolicyIteration(discount, self.env, augmentActionSet=True)
        """Use the direction of the eigenvector as intrinsic reward for the policy iteration algorithm"""
        self.env.define_reward_function(eigenvectors[i])
        """Get the optimal value function and policy"""
        V, pi = polIter.solvePolicyIteration()

        for j in range(len(V)):
          if V[j] < epsilon:
            pi[j] = len(self.env.get_action_set())

        """Plot them"""
        self.plot_value_function(V[0:self.nb_states], str(i) + "_")
        self.plot_policy(pi[0:self.nb_states], str(i) + "_")

  """Plot value functions"""
  def plot_value_function(self, value_function, prefix):
    fig, ax = plt.subplots(subplot_kw=dict(projection='3d'))
    X, Y = np.meshgrid(np.arange(self.config.input_size[1]), np.arange(self.config.input_size[0]))
    reproj_value_function = value_function.reshape(self.config.input_size[0], self.config.input_size[1])

    """Build the support"""
    for i in range(len(X)):
      for j in range(int(len(X[i]) / 2)):
        tmp = X[i][j]
        X[i][j] = X[i][len(X[i]) - j - 1]
        X[i][len(X[i]) - j - 1] = tmp

    cm.jet(np.random.rand(reproj_value_function.shape[0], reproj_value_function.shape[1]))

    ax.plot_surface(X, Y, reproj_value_function, rstride=1, cstride=1,
                    cmap=plt.get_cmap('jet'))
    plt.gca().view_init(elev=30, azim=30)
    plt.savefig(os.path.join(self.v_folder, "SuccessorFeatures" + prefix + 'value_function.png'))
    plt.close()

  """Plot the policy"""
  def plot_policy(self, policy, prefix):
    plt.clf()
    for idx in range(len(policy)):
      i, j = self.env.get_state_xy(idx)

      dx = 0
      dy = 0
      if policy[idx] == 0:  # up
        dy = 0.35
      elif policy[idx] == 1:  # right
        dx = 0.35
      elif policy[idx] == 2:  # down
        dy = -0.35
      elif policy[idx] == 3:  # left
        dx = -0.35
      elif self.env.not_wall(i, j) and policy[idx] == 4:  # termination
        circle = plt.Circle(
          (j + 0.5, self.config.input_size[0] - i + 0.5 - 1), 0.025, color='k')
        plt.gca().add_artist(circle)

      if self.env.not_wall(i, j):
        plt.arrow(j + 0.5, self.config.input_size[0] - i + 0.5 - 1, dx, dy,
                  head_width=0.05, head_length=0.05, fc='k', ec='k')
      else:
        plt.gca().add_patch(
          patches.Rectangle(
            (j, self.config.input_size[0] - i - 1),  # (x,y)
            1.0,  # width
            1.0,  # height
            facecolor="gray"
          )
        )

    plt.xlim([0, self.config.input_size[1]])
    plt.ylim([0, self.config.input_size[0]])

    for i in range(self.config.input_size[1]):
      plt.axvline(i, color='k', linestyle=':')
    plt.axvline(self.config.input_size[1], color='k', linestyle=':')

    for j in range(self.config.input_size[0]):
      plt.axhline(j, color='k', linestyle=':')
    plt.axhline(self.config.input_size[0], color='k', linestyle=':')

    plt.savefig(os.path.join(self.policy_folder, "SuccessorFeatures_" + prefix + 'policy.png'))
    plt.close()

  """Reproject and plot cluster directions"""
  def plot_clusters(self, clusters):
    plt.clf()
    for i in range(len(clusters)):
      reproj_eigenvector = clusters[i].reshape(self.config.input_size[0], self.config.input_size[1])
      """Take both signs"""
      """Plot of the eigenvector"""
      ax = sns.heatmap(reproj_eigenvector, cmap="Blues")

      """Adding borders"""
      for idx in range(self.nb_states):
        ii, jj = self.env.get_state_xy(idx)
        if self.env.not_wall(ii, jj):
          continue
        else:
          plt.gca().add_patch(
            patches.Rectangle(
              (jj, self.config.input_size[0] - ii - 1),  # (x,y)
              1.0,  # width
              1.0,  # height
              facecolor="gray"
            )
          )
      """Saving plots"""
      plt.savefig(os.path.join(self.clusters_folder, ("Direction" + str(i) + '.png')))
      plt.close()

  def print_current_option_direction(self):
    plt.clf()
    reproj_direction = self.current_option_direction.reshape(
      self.config.input_size[0],
      self.config.input_size[1])
    reproj_obs = np.squeeze(self.env.build_screen(), -1)
    clusters = self.global_network.direction_clusters.get_clusters()
    reproj_query = self.query_direction.reshape(
      self.config.input_size[0],
      self.config.input_size[1])
    reproj_state_occupancy = self.episode_state_occupancy.reshape(
      self.config.input_size[0],
      self.config.input_size[1])

    params = {'figure.figsize': (60, 10),
              'axes.titlesize': 'medium',
              }
    plt.rcParams.update(params)

    f = plt.figure(figsize=(25, 5), frameon=False)
    plt.axis('off')
    f.patch.set_visible(False)

    gs0 = gridspec.GridSpec(1, 3)

    gs00 = gridspec.GridSpecFromSubplotSpec(2, 2, subplot_spec=gs0[0])
    gs01 = gridspec.GridSpecFromSubplotSpec(2, 2, subplot_spec=gs0[1])
    gs02 = gridspec.GridSpecFromSubplotSpec(2, 4, subplot_spec=gs0[2])

    ax1 = plt.Subplot(f, gs00[:, :])
    ax1.set_aspect(1.0)
    ax1.axis('off')
    ax1.set_title('Context direction embedding', fontsize=20)
    sns.heatmap(reproj_direction, cmap="Blues", ax=ax1)

    """Adding borders"""
    for idx in range(self.nb_states):
      ii, jj = self.env.get_state_xy(idx)
      if self.env.not_wall(ii, jj):
        continue
      else:
        ax1.add_patch(
          patches.Rectangle(
            (jj, self.config.input_size[0] - ii - 1),  # (x,y)
            1.0,  # width
            1.0,  # height
            facecolor="gray"
          )
        )
    f.add_subplot(ax1)

    ax2 = plt.Subplot(f, gs01[0, 0])
    ax2.set_aspect(1.0)
    ax2.axis('off')
    ax2.set_title('Last observation', fontsize=20)
    sns.heatmap(reproj_obs, cmap="Blues", ax=ax2)

    """Adding borders"""
    for idx in range(self.nb_states):
      ii, jj = self.env.get_state_xy(idx)
      if self.env.not_wall(ii, jj):
        continue
      else:
        ax2.add_patch(
          patches.Rectangle(
            (jj, self.config.input_size[0] - ii - 1),  # (x,y)
            1.0,  # width
            1.0,  # height
            facecolor="gray"
          )
        )
    f.add_subplot(ax2)

    ax3 = plt.Subplot(f, gs01[1, 0])
    ax3.set_aspect(1.0)
    ax3.axis('off')
    ax3.set_title('State occupancy', fontsize=20)
    sns.heatmap(reproj_state_occupancy, cmap="Blues", ax=ax3)

    """Adding borders"""
    for idx in range(self.nb_states):
      ii, jj = self.env.get_state_xy(idx)
      if self.env.not_wall(ii, jj):
        continue
      else:
        ax3.add_patch(
          patches.Rectangle(
            (jj, self.config.input_size[0] - ii - 1),  # (x,y)
            1.0,  # width
            1.0,  # height
            facecolor="gray"
          )
        )
    f.add_subplot(ax3)

    ax4 = plt.Subplot(f, gs01[0, 1])
    ax4.set_aspect(1.0)
    ax4.axis('off')
    ax4.set_title('Query direction embedding', fontsize=20)
    sns.heatmap(reproj_query, cmap="Blues", ax=ax4)

    """Adding borders"""
    for idx in range(self.nb_states):
      ii, jj = self.env.get_state_xy(idx)
      if self.env.not_wall(ii, jj):
        continue
      else:
        ax4.add_patch(
          patches.Rectangle(
            (jj, self.config.input_size[0] - ii - 1),  # (x,y)
            1.0,  # width
            1.0,  # height
            facecolor="gray"
          )
        )
    f.add_subplot(ax4)

    indx = [[0, 0], [0, 1], [0, 2], [0, 3],
            [1, 0], [1, 1], [1, 2], [1, 3]]

    for k in range(len(clusters)):
      reproj_cluster = clusters[k].reshape(
        self.config.input_size[0],
        self.config.input_size[1])

      """Plot of the eigenvector"""
      axn = plt.Subplot(f, gs02[indx[k][0], indx[k][1]])
      axn.set_aspect(1.0)
      axn.axis('off')
      axn.set_title("%.3f/%.3f" % (self.attention_weights[k], self.query_content_match[k]))
      sns.heatmap(reproj_cluster, cmap="Blues", ax=axn)

      """Adding borders"""
      for idx in range(self.nb_states):
        ii, jj = self.env.get_state_xy(idx)
        if self.env.not_wall(ii, jj):
          continue
        else:
          # new_coords = axn.transData.transform()
          axn.add_patch(
            patches.Rectangle(
              (jj, self.config.input_size[0] - ii - 1),  # (x,y)
              1.0,  # width
              1.0,  # height
              facecolor="gray"
              # transform=axn.transAxes,
            )
          )
      f.add_subplot(axn)

    """Saving plots"""
    plt.savefig(os.path.join(self.policy_folder, f'Current_option_direction_{self.global_step_np}_{self.global_episode_np}.png'))
    plt.close()

  def evaluate(self, coord, saver):
    self.saver = saver

    with self.sess.as_default(), self.sess.graph.as_default():
      self.init_agent()
      self.sync_threads()

      task_perf = []
      for goal_location in self.config.goal_locations:
        perf_length = []
        self.env.move_goal_to(goal_location)
        goal_index = self.env.get_state_index(goal_location[0], goal_location[1])
        self.goal_sf = self.sess.run(self.local_network.sf, {self.local_network.observation: np.identity(self.nb_states)[goal_index:goal_index+1],
                                              self.local_network.direction_clusters: self.global_network.direction_clusters.get_clusters()
                                              })

        for _ in range(self.config.nb_test_ep):
          """update local network parameters from global network"""

          self.init_episode()

          """Reset the environment and get the initial state"""
          s = self.env.get_initial_state()

          """While the episode does not terminate"""
          while not self.done:
            """update local network parameters from global network"""
            self.sync_threads()

            """Choose an action from the current intra-option policy"""
            self.policy_evaluation(s)

            _, r, self.done, s1 = self.env.special_step(self.action, s)

            self.reward = r
            self.episode_reward += self.reward

            """If the episode ended make the last state absorbing"""
            if self.done:
              s1 = s

            self.reward_mix = self.reward

            self.episode_mixed_reward += self.reward_mix

            s = s1
            self.episode_length += 1
            self.total_steps += 1

          print(f"Episode length {self.episode_length} for goal location {goal_location}")
          perf_length.append(self.episode_length)

        task_performance = np.mean(perf_length)
        task_perf.append(task_performance)

    plt.clf()
    plt.bar(f"{self.config.goal_locations[0]}, {self.config.goal_locations[1]}", task_perf, 1/1.5, color="blue")
    plt.savefig(os.path.join(self.learning_progress_folder, f'Learning_progress.png'))
    plt.close()

  def save_model(self):
    self.saver.save(self.sess, self.model_path + '/model-{}.cptk'.format(self.global_episode_np),
                    global_step=self.global_episode)
    tf.logging.info(
      "Saved Model at {}".format(self.model_path + '/model-{}.cptk'.format(self.global_episode_np)))

    direction_clusters_path = os.path.join(self.cluster_model_path, "direction_clusters_{}.pkl".format(self.global_episode_np))
    f = open(direction_clusters_path, 'wb')
    pickle.dump(self.global_network.direction_clusters, f, protocol=pickle.HIGHEST_PROTOCOL)
    f.close()

