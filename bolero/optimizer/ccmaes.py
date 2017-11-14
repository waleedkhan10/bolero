# Authors: Alexander Fabisch <afabisch@informatik.uni-bremen.de>

import numpy as np
from scipy.optimize import fmin_l_bfgs_b
from collections import deque
from ..optimizer import ContextualOptimizer
from ..utils.mathext import logsumexp
from ..utils.scaling import Scaling
from ..representation.ul_policies import (ContextTransformationPolicy,
                                          LinearGaussianPolicy,
                                          BoundedScalingPolicy)
from ..utils.validation import check_random_state, check_feedback, check_context
from ..utils.log import get_logger
from .creps import solve_dual_contextual_reps


class CCMAESOptimizer(ContextualOptimizer):
    """Contextual Covariance Matrix Adaptation Evolution Strategy.

    This contextual version of :class:`~bolero.optimizer.CMAESOptimizer`
    inherits the properties from the original algorithm. More information
    on the algorithm can be found in the original publication [1]_.

    Parameters
    ----------
    initial_params : array-like, shape (n_params,)
        Initial parameter vector.

    variance : float, optional (default: 1.0)
        Initial exploration variance.

    covariance : array-like, optional (default: None)
        Either a diagonal (with shape (n_params,)) or a full covariance matrix
        (with shape (n_params, n_params)). A full covariance can contain
        information about the correlation of variables.

    epsilon : float, optional (default: 2.0)
        Maximum Kullback-Leibler divergence of two successive policy
        distributions.

    min_eta : float, optional (default: 1e-8)
        Minimum eta, 0 would result in numerical problems

    n_samples_per_update : int, optional
        Number of samples that will be used to update a policy.
        default: 4 + int(3*log(n_params + n_context_dims)) *
                 (1 + 2 * n_context_dims)

    context_features : string or callable, optional (default: None)
        (Nonlinear) feature transformation for the context.

    gamma : float, optional (default: 1e-4)
        Regularization parameter. Should be removed in the future.

    log_to_file: optional, boolean or string (default: False)
        Log results to given file, it will be located in the $BL_LOG_PATH

    log_to_stdout: optional, boolean (default: False)
        Log to standard output

    random_state : optional, int
        Seed for the random number generator.

    References
    ----------
    .. [1] Abdolmaleki, A.; Price, B.; Lau, N.; Paulo Reis, L.; Neumann, G.
        Contextual Covariance Matrix Adaptation Evolution Strategies.
    """
    def __init__(self, initial_params=None, variance=None, covariance=None,
                 epsilon=2.0, min_eta=1e-8, n_samples_per_update=None,
                 context_features=None, gamma=1e-4,
                 log_to_file=False, log_to_stdout=False,
                 random_state=None, **kwargs):
        self.initial_params = initial_params
        self.variance = variance
        self.covariance = covariance
        self.epsilon = epsilon
        self.min_eta = min_eta
        self.n_samples_per_update = n_samples_per_update
        self.context_features = context_features
        self.gamma = gamma
        self.log_to_file = log_to_file
        self.log_to_stdout = log_to_stdout
        self.random_state = random_state

    def init(self, n_params, n_context_dims):
        """Initialize optimizer.

        Parameters
        ----------
        n_params : int
            number of parameters

        n_context_dims : int
            number of dimensions of the context space
        """
        self.logger = get_logger(self, self.log_to_file, self.log_to_stdout)

        self.random_state = check_random_state(self.random_state)

        self.it = 0

        if self.initial_params is None:
            self.initial_params = np.zeros(n_params)
        else:
            self.initial_params = np.asarray(self.initial_params).astype(
                np.float64, copy=True)
        if n_params != len(self.initial_params):
            raise ValueError("Number of dimensions (%d) does not match "
                             "number of initial parameters (%d)."
                             % (n_params, len(self.initial_params)))

        self.context = None
        self.params = None
        self.reward = None

        self.policy_ = ContextTransformationPolicy(
            LinearGaussianPolicy, n_params, n_context_dims,
            context_transformation=self.context_features,
            mean=inv_scaled_params, covariance_scale=1.0, gamma=self.gamma,
            random_state=self.random_state)
        self.policy_.policy.Sigma = self.variance * self.covariance

        self.n_total_dims = n_params + n_context_dims
        if self.n_samples_per_update is None:
            self.n_samples_per_update = (
                4 + int(3 * np.log(self.n_total_dims)) *
                (1 + 2 * n_context_dims))

        # TODO don't know if n_params or n_total_dims
        self.hsig_threshold = 2 + 4.0 / (self.n_params + 1)

        self.history_theta = deque(maxlen=self.n_samples_per_update)
        self.history_R = deque(maxlen=self.n_samples_per_update)
        self.history_s = deque(maxlen=self.n_samples_per_update)
        self.history_phi_s = deque(maxlen=self.n_samples_per_update)

        self.var = self.variance

        # Evolution path for covariance
        self.pc = np.zeros(self.n_params)
        # Evolution path for sigma
        self.ps = np.zeros(self.n_params)

        # TODO

    def get_desired_context(self):
        """C-REPS does not actively select the context.

        Returns
        -------
        context : None
            C-REPS does not have any preference
        """
        return None

    def set_context(self, context):
        """Set context of next evaluation.

        Parameters
        ----------
        context : array-like, shape (n_context_dims,)
            The context in which the next rollout will be performed
        """
        self.context = check_context(context)

    def get_next_parameters(self, params, explore=True):
        """Get next individual/parameter vector for evaluation.

        Parameters
        ----------
        params : array_like, shape (n_params,)
            Parameter vector, will be modified

        explore : bool, optional (default: True)
            Whether we want to turn exploration on for the next evaluation
        """
        self.params = self.policy_(self.context, explore=explore)
        params[:] = self.params

    def set_evaluation_feedback(self, rewards):
        """Set feedbacks for the parameter vector.

        Parameters
        ----------
        rewards : list of float
            Feedbacks for each step or for the episode, depends on the problem
        """
        self._add_sample(rewards)

        if self.it % self.n_samples_per_update == 0:
            phi_s = np.asarray(self.history_phi_s)
            theta = np.asarray(self.history_theta)
            R = np.asarray(self.history_R)

            weights = solve_dual_contextual_reps(
                phi_s, R, self.epsilon, self.min_eta)[0]

            # Number of effectice samples depends on weights
            mu_w = int(1.0 / np.sum(weights ** 2))
            self.logger.info("[CCMAES] %d active samples" % mu)

            c1 = (2 * min(1, int(self.n_samples_per_update / 6.0))) / (
                (self.n_total_dims + 1.3) ** 2 + mu_w)
            cmu = 2 * (mu_w - 2 + 1.0 / mu_w) / (
                (self.n_total_dims + 2) ** 2 + mu_w)
            cc = 4.0 / (4.0 + self.n_total_dims)

            c_sigma = (mu_w + 2) / float(self.n_total_dims + mu_w + 3)
            d_sigma = (1 + c_sigma
                       + 2 * np.sqrt((mu_w - 1) / (self.n_total_dims + 1))
                       - 2 + np.log(1 + 2 * self.n_total_dims))

            last_W = np.copy(self.policy_.W)
            self.policy_.fit(phi_s, theta, d, context_transform=False)

            mean_phi = np.mean(phi_s, axis=0)
            sigma = np.sqrt(self.var)
            mean_diff = (self.policy_.W.dot(mean_phi) - last_W.dot(mean_phi)) / sigma

            self.ps *= (1.0 - c_sigma)

            # TODO refactor?
            cov = np.copy(self.policy_.policy.Sigma)
            cov[:, :] = np.triu(cov) + np.triu(cov, 1).T
            D, B = np.linalg.eigh(cov)
            # HACK: avoid numerical problems
            D = np.maximum(D, np.finfo(np.float).eps)
            D = np.diag(np.sqrt(1.0 / D))
            invsqrtC = B.dot(D).dot(B.T)

            self.ps += (np.sqrt(c_sigma * (2.0 - c_sigma)) * np.sqrt(mu_w) *
                        invsqrtC.dot(mean_diff))

            ps_norm_2 = np.linalg.norm(self.ps) ** 2  # Temporary constant
            generation = it / self.n_samples_per_update
            hsig = int(ps_norm_2 / self.n_params /
                    np.sqrt(1 - (1 - c_sigma) ** (2 * generation))
                    < self.hsig_threshold)
            self.pc *= 1.0 - cc
            self.pc += hsig * np.sqrt(cc * (2.0 - cc)) * np.sqrt(mu_w) * mean_diff

            # Rank-1 update
            rank_one_update = np.outer(self.pc, self.pc)

            # Rank-mu update
            noise = (theta - last_W.dot(mean_phi)) / sigma
            # TODO refactor: compute with var instead of sigma?
            rank_mu_update = noise.T.dot(np.diag(weights)).dot(noise)
            cov *= (1.0 - c1 - cmu)
            cov += cmu * rank_mu_update
            cov += c1 * rank_one_update

            # Adapt step size with factor <= exp(0.6)
            log_step_size_update = ((c_sigma / d_sigma) *
                                    (ps_norm_2 / self.n_params - 1))
            self.var *= np.exp(np.min((0.6, log_step_size_update))) ** 2

    def _add_sample(self, rewards):
        self.reward = check_feedback(rewards, compute_sum=True)
        self.logger.info("Reward %.6f" % self.reward)

        inv_scaled_params = self.scaler.inv_scale(self.params)
        phi_s = self.policy_.transform_context(self.context)

        self.history_theta.append(inv_scaled_params)
        self.history_R.append(self.reward)
        self.history_s.append(self.context)
        self.history_phi_s.append(phi_s)

        self.it += 1

    def best_policy(self):
        """Return current best estimate of contextual policy.

        Returns
        -------
        policy : UpperLevelPolicy
            Best estimate of upper-level policy
        """
        return self.policy_

    def is_behavior_learning_done(self):
        """Check if the optimization is finished.

        Returns
        -------
        finished : bool
            Is the learning of a behavior finished?
        """
        return False

    def __getstate__(self):
        d = dict(self.__dict__)
        del d["logger"]
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.logger = get_logger(self, self.log_to_file, self.log_to_stdout)
