"""Stochastic Limited Memory Bundle Algorithm (SLMBA) on Metal GPU.

Implements nonsmooth, nonconvex optimization for continuous clustering
using hardware-accelerated PyTorch Metal (MPS) with automatic CUDA/CPU fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import torch

logger = logging.getLogger(__name__)


@dataclass
class SLMBAParameters:
    """Hyperparameters for Stochastic Limited Memory Bundle Algorithm."""

    max_iter: int = 300
    bundle_size: int = 7
    line_search_max_step: float = 1.0
    line_search_c1: float = 1e-4
    backtrack_factor: float = 0.5
    max_backtracks: int = 15
    grad_tol: float = 1e-4
    func_tol: float = 1e-7
    non_monotone_window: int = 5
    batch_rotation_interval: int = 10


class MetalSLMBAOptimizer:
    """Metal (MPS) accelerated Stochastic Limited Memory Bundle Optimizer.

    Solves unconstrained continuous nonsmooth optimization problems of the form:
        min_{X} f(X)
    where f is locally Lipschitz and non-differentiable (nonsmooth).
    """

    def __init__(
        self,
        params: Optional[SLMBAParameters] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        self.params = params or SLMBAParameters()
        if device is not None:
            self.device = device
        elif torch.backends.mps.is_available():
            self.device = torch.device("mps")
        elif torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")

    def optimize_clustering(
        self,
        init_centers: torch.Tensor,
        data: torch.Tensor,
        batch_size: Optional[int] = None,
    ) -> Tuple[torch.Tensor, float]:
        """Optimize cluster centers X in R^(k x d) on dataset A in R^(N x d).

        Args:
            init_centers: [k, d] initial centroids tensor.
            data: [N, d] dataset tensor.
            batch_size: Optional stochastic batch size B <= N.

        Returns:
            (best_centers, best_loss): Optimized centroids and objective value.
        """
        X = init_centers.clone().to(self.device).float()
        A = data.to(self.device).float()
        N, d = A.shape
        k = X.shape[0]

        B = min(batch_size or N, N)
        use_minibatch = B < N

        # History buffers for L-BFGS two-loop recursion
        m_c = self.params.bundle_size
        s_history: list[torch.Tensor] = []  # delta X
        y_history: list[torch.Tensor] = []  # delta grad
        rho_history: list[float] = []

        # Non-monotone objective history window
        f_history: list[float] = []

        def _get_batch() -> torch.Tensor:
            if not use_minibatch:
                return A
            idx = torch.randint(0, N, (B,), device=self.device)
            return A[idx]

        curr_batch = _get_batch()

        # Initial evaluation
        loss, grad = self._eval_cluster_loss_and_grad(X, curr_batch)
        f_history.append(loss)

        best_X = X.clone()
        best_loss = loss

        for iteration in range(self.params.max_iter):
            # Rotate stochastic minibatch every N_th iterations
            if use_minibatch and (iteration % self.params.batch_rotation_interval == 0) and iteration > 0:
                curr_batch = _get_batch()
                loss, grad = self._eval_cluster_loss_and_grad(X, curr_batch)

            g_norm = grad.norm().item()
            if g_norm < self.params.grad_tol:
                logger.debug("SLMBA converged on grad norm: %e at iter %d", g_norm, iteration)
                break

            # 1. Compute search direction d_k via L-BFGS two-loop recursion
            d_k = self._compute_lbfgs_direction(grad, s_history, y_history, rho_history)

            # Ensure angle with subgradient is negative (descent condition)
            dir_dot_grad = (d_k * grad).sum().item()
            if dir_dot_grad >= -1e-5 * (d_k.norm().item() * g_norm + 1e-12):
                # Reset direction to steepest subgradient descent
                d_k = -grad
                s_history.clear()
                y_history.clear()
                rho_history.clear()
                dir_dot_grad = -(g_norm * g_norm)

            # 2. Non-monotone line search
            alpha = self.params.line_search_max_step
            f_ref = max(f_history[-self.params.non_monotone_window :])
            step_accepted = False
            new_X = X
            new_loss = loss
            new_grad = grad

            for _ in range(self.params.max_backtracks):
                trial_X = X + alpha * d_k
                trial_loss, trial_grad = self._eval_cluster_loss_and_grad(trial_X, curr_batch)

                # Armijo condition with non-monotone reference
                if trial_loss <= f_ref + self.params.line_search_c1 * alpha * dir_dot_grad:
                    step_accepted = True
                    new_X = trial_X
                    new_loss = trial_loss
                    new_grad = trial_grad
                    break
                alpha *= self.params.backtrack_factor

            if not step_accepted:
                # Null step: iterate does not advance, but gradient is added to bundle
                trial_X = X + (self.params.line_search_max_step * 0.1) * d_k
                _, trial_grad = self._eval_cluster_loss_and_grad(trial_X, curr_batch)
                s_k = trial_X - X
                y_k = trial_grad - grad
            else:
                s_k = new_X - X
                y_k = new_grad - grad
                X = new_X
                loss = new_loss
                grad = new_grad
                f_history.append(loss)
                if loss < best_loss:
                    best_loss = loss
                    best_X = X.clone()

            # 3. Update L-BFGS bundle history if curvature condition holds
            sy = (s_k * y_k).sum().item()
            if sy > 1e-10:
                if len(s_history) >= m_c:
                    s_history.pop(0)
                    y_history.pop(0)
                    rho_history.pop(0)
                s_history.append(s_k)
                y_history.append(y_k)
                rho_history.append(1.0 / sy)

            # Check relative function improvement
            if len(f_history) >= 2:
                rel_diff = abs(f_history[-1] - f_history[-2]) / max(abs(f_history[-1]), 1.0)
                if rel_diff < self.params.func_tol:
                    break

        return best_X, best_loss

    def optimize_auxiliary(
        self,
        candidate_center: torch.Tensor,
        data: torch.Tensor,
        d_min_prev: torch.Tensor,
        batch_size: Optional[int] = None,
    ) -> Tuple[torch.Tensor, float]:
        """Optimize a single new candidate center y in R^(1 x d) for the auxiliary problem.

        min_y sum_i min(d_min_prev(a_i), ||a_i - y||^2)
        """
        y = candidate_center.clone().to(self.device).float()
        if y.ndim == 1:
            y = y.unsqueeze(0)
        A = data.to(self.device).float()
        d_prev = d_min_prev.to(self.device).float()
        N, d = A.shape

        B = min(batch_size or N, N)
        use_minibatch = B < N

        m_c = self.params.bundle_size
        s_history: list[torch.Tensor] = []
        y_history: list[torch.Tensor] = []
        rho_history: list[float] = []
        f_history: list[float] = []

        def _get_batch():
            if not use_minibatch:
                return A, d_prev
            idx = torch.randint(0, N, (B,), device=self.device)
            return A[idx], d_prev[idx]

        curr_A, curr_d_prev = _get_batch()
        loss, grad = self._eval_aux_loss_and_grad(y, curr_A, curr_d_prev)
        f_history.append(loss)
        best_y = y.clone()
        best_loss = loss

        for iteration in range(self.params.max_iter // 2):
            if use_minibatch and (iteration % self.params.batch_rotation_interval == 0) and iteration > 0:
                curr_A, curr_d_prev = _get_batch()
                loss, grad = self._eval_aux_loss_and_grad(y, curr_A, curr_d_prev)

            g_norm = grad.norm().item()
            if g_norm < self.params.grad_tol:
                break

            d_k = self._compute_lbfgs_direction(grad, s_history, y_history, rho_history)
            dir_dot_grad = (d_k * grad).sum().item()
            if dir_dot_grad >= -1e-5 * (d_k.norm().item() * g_norm + 1e-12):
                d_k = -grad
                s_history.clear()
                y_history.clear()
                rho_history.clear()
                dir_dot_grad = -(g_norm * g_norm)

            alpha = self.params.line_search_max_step
            f_ref = max(f_history[-self.params.non_monotone_window :])
            step_accepted = False
            new_y, new_loss, new_grad = y, loss, grad

            for _ in range(self.params.max_backtracks):
                trial_y = y + alpha * d_k
                trial_loss, trial_grad = self._eval_aux_loss_and_grad(trial_y, curr_A, curr_d_prev)
                if trial_loss <= f_ref + self.params.line_search_c1 * alpha * dir_dot_grad:
                    step_accepted = True
                    new_y, new_loss, new_grad = trial_y, trial_loss, trial_grad
                    break
                alpha *= self.params.backtrack_factor

            if not step_accepted:
                trial_y = y + (self.params.line_search_max_step * 0.1) * d_k
                _, trial_grad = self._eval_aux_loss_and_grad(trial_y, curr_A, curr_d_prev)
                s_k = trial_y - y
                y_k = trial_grad - grad
            else:
                s_k = new_y - y
                y_k = new_grad - grad
                y = new_y
                loss = new_loss
                grad = new_grad
                f_history.append(loss)
                if loss < best_loss:
                    best_loss = loss
                    best_y = y.clone()

            sy = (s_k * y_k).sum().item()
            if sy > 1e-10:
                if len(s_history) >= m_c:
                    s_history.pop(0)
                    y_history.pop(0)
                    rho_history.pop(0)
                s_history.append(s_k)
                y_history.append(y_k)
                rho_history.append(1.0 / sy)

            if len(f_history) >= 2:
                rel_diff = abs(f_history[-1] - f_history[-2]) / max(abs(f_history[-1]), 1.0)
                if rel_diff < self.params.func_tol:
                    break

        return best_y, best_loss

    def _eval_cluster_loss_and_grad(
        self, X: torch.Tensor, A: torch.Tensor
    ) -> Tuple[float, torch.Tensor]:
        """Compute full MSSC loss and subgradient on Metal GPU."""
        # ||a_i - x_j||^2 = ||a_i||^2 - 2 <a_i, x_j> + ||x_j||^2
        norm_A = (A * A).sum(dim=1, keepdim=True)  # [B, 1]
        norm_X = (X * X).sum(dim=1, keepdim=True).T  # [1, k]
        # Cross term via GEMM
        D = norm_A - 2.0 * torch.matmul(A, X.T) + norm_X  # [B, k]

        min_dist, j_star = torch.min(D, dim=1)  # [B], [B]
        loss = min_dist.sum().item()

        # Subgradient: G_j = 2 * sum_{i: j*(i)=j} (x_j - a_i)
        grad = torch.zeros_like(X)
        diff = X[j_star] - A
        grad.index_add_(0, j_star, 2.0 * diff)
        return loss, grad

    def _eval_aux_loss_and_grad(
        self, y: torch.Tensor, A: torch.Tensor, d_prev: torch.Tensor
    ) -> Tuple[float, torch.Tensor]:
        """Compute auxiliary loss and subgradient for single new center on Metal GPU."""
        diff = A - y  # [B, d]
        dist_y = (diff * diff).sum(dim=1)  # [B]
        better_mask = dist_y < d_prev  # [B] bool

        loss = torch.where(better_mask, dist_y, d_prev).sum().item()

        grad = torch.zeros_like(y)
        if better_mask.any():
            grad = 2.0 * (y - A[better_mask]).sum(dim=0, keepdim=True)
        return loss, grad

    def _compute_lbfgs_direction(
        self,
        grad: torch.Tensor,
        s_history: list[torch.Tensor],
        y_history: list[torch.Tensor],
        rho_history: list[float],
    ) -> torch.Tensor:
        """Two-loop L-BFGS recursion to compute quasi-Newton search direction."""
        if not s_history:
            return -grad

        q = grad.clone()
        m = len(s_history)
        alphas = [0.0] * m

        for i in reversed(range(m)):
            alphas[i] = rho_history[i] * (s_history[i] * q).sum().item()
            q -= alphas[i] * y_history[i]

        # Initial Hessian approximation scale
        s_last = s_history[-1]
        y_last = y_history[-1]
        gamma = (s_last * y_last).sum().item() / max((y_last * y_last).sum().item(), 1e-12)
        r = gamma * q

        for i in range(m):
            beta = rho_history[i] * (y_history[i] * r).sum().item()
            r += s_history[i] * (alphas[i] - beta)

        return -r
