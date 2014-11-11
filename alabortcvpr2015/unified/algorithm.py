from __future__ import division
import abc

import numpy as np
from numpy.fft import fft2, ifft2, fftshift

from menpofast.feature import gradient as fast_gradient
from menpofast.utils import build_parts_image
from menpofast.image import Image

from menpofit.base import build_sampling_grid

from .result import UnifiedAlgorithmResult

multivariate_normal = None  # expensive, from scipy.stats


# Abstract Interface for AAM Algorithms ---------------------------------------

class UnifiedAlgorithm(object):

    __metaclass__ = abc.ABCMeta

    def __init__(self, aam_interface, appearance_model, transform,
                 multiple_clf, parts_shape, normalize_parts, covariance, pdm,
                 scale=10, factor=100, eps=10**-5, **kwargs):

        # AAM part ------------------------------------------------------------

        # set state
        self.appearance_model = appearance_model
        self.template = appearance_model.mean()
        self.transform = transform
        # set interface
        self.interface = aam_interface(self, **kwargs)
        # mask appearance model
        self._U = self.appearance_model.components.T
        self._pinv_U = np.linalg.pinv(
            self._U[self.interface.image_vec_mask, :]).T

        # CLM part ------------------------------------------------------------

        # set state
        self.multiple_clf = multiple_clf
        self.parts_shape = parts_shape
        self.normalize_parts = normalize_parts
        self.covariance = covariance
        self.pdm = pdm
        self.scale = scale
        self.factor = factor

        # Unified -------------------------------------------------------------

        self.eps = eps
        # pre-compute
        self._precompute()

    @abc.abstractmethod
    def _precompute(self, **kwargs):
        pass

    @abc.abstractmethod
    def run(self, image, initial_shape, max_iters=20, gt_shape=None, **kwargs):
        pass


# Concrete Implementations of AAM Algorithm -----------------------------------

class PIC_RLMS(UnifiedAlgorithm):
    r"""
    Project-Out Inverse Compositional Algorithm
    """

    def _precompute(self):

        # AAM part ------------------------------------------------------------

        # sample appearance model
        self._U = self._U[self.interface.image_vec_mask, :]

        # compute model's gradient
        nabla_t = self.interface.gradient(self.template)

        # compute warp jacobian
        dw_dp = self.interface.dw_dp()

        # set sigma2
        self._sigma2 = self.appearance_model.noise_variance()

        # compute steepest descent images
        j = self.interface.steepest_descent_images(nabla_t, dw_dp)

        # project out appearance model from J
        self._j_aam = j - self._U.dot(self._pinv_U.T.dot(j))

        # compute inverse hessian
        self._h_aam = self._j_aam.T.dot(j)

        # CLM part ------------------------------------------------------------

        global multivariate_normal
        if multivariate_normal is None:
            from scipy.stats import multivariate_normal  # expensive

        # build sampling grid associated to patch shape
        self._sampling_grid = build_sampling_grid(self.parts_shape)
        up_sampled_shape = self.factor * (np.asarray(self.parts_shape) + 1)
        self._up_sampled_grid = build_sampling_grid(up_sampled_shape)
        self.offset = np.mgrid[self.factor:up_sampled_shape[0]:self.factor,
                               self.factor:up_sampled_shape[1]:self.factor]
        self.offset = self.offset.swapaxes(0, 2).swapaxes(0, 1)

        # set rho2
        self._rho2 = self.pdm.model.noise_variance()

        # compute Gaussian-KDE grid
        mean = np.zeros(self.transform.n_dims)
        covariance = self.scale * self._rho2
        mvn = multivariate_normal(mean=mean, cov=covariance)
        self._kernel_grid = mvn.pdf(self._up_sampled_grid/self.factor)
        n_parts = self.transform.model.mean().n_points
        self._kernel_grids = np.empty((n_parts,) + self.parts_shape)

        # compute Jacobian
        j_clm = np.rollaxis(self.pdm.d_dp(None), -1, 1)
        self._j_clm = j_clm.reshape((-1, j_clm.shape[-1]))

        # compute Hessian inverse
        self._h_clm = self._j_clm.T.dot(self._j_clm)

        # Unified part --------------------------------------------------------

        # set Prior
        sim_prior = np.zeros((4,))
        transform_prior = (self._rho2 * self._sigma2 /
                           self.pdm.model.eigenvalues)
        self._j_prior = np.hstack((sim_prior, transform_prior))
        self._h_prior = np.diag(self._j_prior)

    def run(self, image, initial_shape, gt_shape=None, max_iters=20,
            prior=False, a=0.5):

        # initialize transform
        self.transform.set_target(initial_shape)
        shape_parameters = [self.transform.as_vector()]
        # masked model mean
        masked_m = self.appearance_model.mean().as_vector()[
            self.interface.image_vec_mask]

        for _ in xrange(max_iters):

            # AAM part --------------------------------------------------------

            # compute warped image with current weights
            i = self.interface.warp(image)

            # reconstruct appearance
            masked_i = i.as_vector()[self.interface.image_vec_mask]

            # compute error image
            e_aam = masked_m - masked_i

            # CLM part --------------------------------------------------------

            target = self.transform.target
            # get all (x, y) pairs being considered
            xys = (target.points[:, None, None, ...] +
                   self._sampling_grid)

            diff = np.require(
                np.round((np.round(target.points) - target.points) *
                         self.factor),
                dtype=int)

            offsets = diff[:, None, None, :] + self.offset
            for j, o in enumerate(offsets):
                self._kernel_grids[j, ...] = self._kernel_grid[o[..., 0],
                                                               o[..., 1]]

            # build parts image
            parts_image = build_parts_image(
                image, target, parts_shape=self.parts_shape,
                normalize_parts=self.normalize_parts)

            # compute parts response
            parts_response = self.multiple_clf(parts_image)
            parts_response[np.logical_not(np.isfinite(parts_response))] = .5

            # compute parts kernel
            parts_kernel = parts_response * self._kernel_grids
            parts_kernel /= np.sum(
                parts_kernel, axis=(-2, -1))[..., None, None]

            # compute mean shift target
            mean_shift_target = np.sum(parts_kernel[..., None] * xys,
                                       axis=(-3, -2))

            # compute (shape) error term
            e_clm = mean_shift_target.ravel() - target.as_vector()

            # Unified ---------------------------------------------------------

            # compute gauss-newton parameter updates
            if prior:
                h = (self._rho2 * self._h_aam +
                     self._sigma2 * self._h_clm +
                     self._h_prior)
                b = (self._j_prior * self.transform.as_vector() -
                     self._rho2 * self._j_aam.T.dot(e_aam) -
                     self._sigma2 * self._j_clm.T.dot(e_clm))
                dp = -np.linalg.solve(h, b)
            else:
                dp = np.linalg.solve(
                    a * self._h_aam +
                    (1 - a) * self._h_clm,
                    a * self._j_aam.T.dot(e_aam) +
                    (1 - a) * self._j_clm.T.dot(e_clm))

            # update transform
            target = self.transform.target
            self.transform.from_vector_inplace(self.transform.as_vector() + dp)
            shape_parameters.append(self.transform.as_vector())

            # test convergence
            error = np.abs(np.linalg.norm(
                target.points - self.transform.target.points))
            if error < self.eps:
                break

        # return dm algorithm result
        return UnifiedAlgorithmResult(image, self, shape_parameters,
                                      gt_shape=gt_shape)


class AIC_RLMS(UnifiedAlgorithm):
    r"""
    Alternating Inverse Compositional Algorithm
    """

    def _precompute(self):

        # AAM part ------------------------------------------------------------

        # compute warp jacobian
        self._dw_dp = self.interface.dw_dp()

        # set sigma2
        self._sigma2 = self.appearance_model.noise_variance()

        # CLM part ------------------------------------------------------------

        global multivariate_normal
        if multivariate_normal is None:
            from scipy.stats import multivariate_normal  # expensive

        # build sampling grid associated to patch shape
        self._sampling_grid = build_sampling_grid(self.parts_shape)
        up_sampled_shape = self.factor * (np.asarray(self.parts_shape) + 1)
        self._up_sampled_grid = build_sampling_grid(up_sampled_shape)
        self.offset = np.mgrid[self.factor:up_sampled_shape[0]:self.factor,
                               self.factor:up_sampled_shape[1]:self.factor]
        self.offset = self.offset.swapaxes(0, 2).swapaxes(0, 1)

        # set rho2
        self._rho2 = self.pdm.model.noise_variance()

        # compute Gaussian-KDE grid
        mean = np.zeros(self.transform.n_dims)
        covariance = self.covariance * (1 / self._rho2)
        mvn = multivariate_normal(mean=mean, cov=covariance)
        self._kernel_grid = mvn.pdf(self._up_sampled_grid/self.factor)
        n_parts = self.transform.model.mean().n_points
        self._kernel_grids = np.empty((n_parts,) + self.parts_shape)

        # compute Jacobian
        j_clm = np.rollaxis(self.pdm.d_dp(None), -1, 1)
        j_clm = j_clm.reshape((-1, j_clm.shape[-1]))
        self._j_clm = (1 / self._rho2) * j_clm

        # compute Hessian inverse
        self._h_clm = self._j_clm.T.dot(j_clm)

        # Unified part --------------------------------------------------------

        # set Prior
        sim_prior = np.zeros((4,))
        transform_prior = (1 /
                           self.pdm.model.eigenvalues)
        self._j_prior = np.hstack((sim_prior, transform_prior))
        self._h_prior = np.diag(self._j_prior)

    def run(self, image, initial_shape, gt_shape=None, max_iters=20,
            prior=False, a=0.5):

        # initialize transform
        self.transform.set_target(initial_shape)
        shape_parameters = [self.transform.as_vector()]
        # initial appearance parameters
        appearance_parameters = [0]
        # model mean
        m = self.appearance_model.mean().as_vector()
        # masked model mean
        masked_m = m[self.interface.image_vec_mask]

        for _ in xrange(max_iters):

            # AAM part --------------------------------------------------------

            # warp image
            i = self.interface.warp(image)
            # mask image
            masked_i = i.as_vector()[self.interface.image_vec_mask]

            # reconstruct appearance
            c = self._pinv_U.T.dot(masked_i - masked_m)
            t = self._U.dot(c) + m
            self.template.from_vector_inplace(t)
            appearance_parameters.append(c)

            # compute error image
            e_aam = (self.template.as_vector()[self.interface.image_vec_mask] -
                     masked_i)

            # compute model gradient
            nabla_t = self.interface.gradient(self.template)

            # compute model jacobian
            j = self.interface.steepest_descent_images(nabla_t, self._dw_dp)
            j_aam = (1 / self._rho2) * j

            # compute hessian
            h_aam = j_aam.T.dot(j)

            # CLM part --------------------------------------------------------

            target = self.transform.target
            # get all (x, y) pairs being considered
            xys = (target.points[:, None, None, ...] +
                   self._sampling_grid)

            diff = np.require(
                np.round((-np.round(target.points) + target.points) *
                self.factor), dtype=int)

            offsets = diff[:, None, None, :] + self.offset
            for j, o in enumerate(offsets):
                self._kernel_grids[j, ...] = self._kernel_grid[o[..., 0],
                                                               o[..., 1]]

            # build parts image
            parts_image = build_parts_image(
                image, target, parts_shape=self.parts_shape,
                normalize_parts=self.normalize_parts)

            # compute parts response
            parts_response = self.multiple_clf(parts_image)
            parts_response[np.logical_not(np.isfinite(parts_response))] = 1

            # compute parts kernel
            parts_kernel = parts_response * self._kernel_grids
            parts_kernel /= np.sum(
                parts_kernel, axis=(-2, -1))[..., None, None]

            # compute mean shift target
            mean_shift_target = np.sum(parts_kernel[..., None] * xys,
                                       axis=(-3, -2))

            # compute (shape) error term
            e_clm = mean_shift_target.ravel() - target.as_vector()

            # Unified ---------------------------------------------------------

            # compute gauss-newton parameter updates
            if prior:
                h = (h_aam +
                     self._h_clm +
                     self._h_prior)
                b = (self.transform.as_vector() -
                     j_aam.T.dot(e_aam) -
                     self._j_clm.T.dot(e_clm))
                dp = -np.linalg.solve(h, b)
            else:
                dp = np.linalg.solve(
                    h_aam +
                    self._h_clm,
                    j_aam.T.dot(e_aam) +
                    self._j_clm.T.dot(e_clm))

            # update transform
            target = self.transform.target
            self.transform.from_vector_inplace(self.transform.as_vector() + dp)
            shape_parameters.append(self.transform.as_vector())

            # test convergence
            error = np.abs(np.linalg.norm(
                target.points - self.transform.target.points))
            if error < self.eps:
                break

        # return Unified algorithm result
        return UnifiedAlgorithmResult(
            image, self, shape_parameters,
            appearance_parameters=appearance_parameters, gt_shape=gt_shape)


class PIC_LKInverse(UnifiedAlgorithm):
    r"""
    Project-Out Inverse Compositional Algorithm
    """

    def __init__(self, aam_interface, appearance_model, transform,
                 multiple_clf, parts_shape, normalize_parts, covariance, pdm,
                 eps=10**-5, **kwargs):

        # AAM part ------------------------------------------------------------

        # set state
        self.appearance_model = appearance_model
        self.template = appearance_model.mean()
        self.transform = transform
        # set interface
        self.interface = aam_interface(self, **kwargs)
        # mask appearance model
        self._U = self.appearance_model.components.T
        self._pinv_U = np.linalg.pinv(
            self._U[self.interface.image_vec_mask, :]).T

        # CLM part ------------------------------------------------------------

        # set state
        self.multiple_clf = multiple_clf
        self.parts_shape = parts_shape
        self.normalize_parts = normalize_parts
        self.covariance = covariance
        self.pdm = pdm

        self.n_parts = self.multiple_clf.F.shape[0]
        self.n_channels = self.multiple_clf.F.shape[-3]

        global multivariate_normal
        if multivariate_normal is None:
            from scipy.stats import multivariate_normal  # expensive

        # compute template response
        mvn = multivariate_normal(mean=np.zeros(2), cov=self.covariance)
        grid = build_sampling_grid(self.parts_shape)
        self.template2 = np.require(
            fft2(np.tile(mvn.pdf(grid)[None, None, None],
                         (self.n_parts, 1, 1, 1, 1))), dtype=np.complex64)

        sampling_mask = kwargs.get('sampling_mask')

        if sampling_mask is None:
            parts_shape = self.parts_shape
            sampling_mask = np.require(np.ones((parts_shape)), dtype=np.bool)

        template_shape = self.template2.shape
        image_mask = np.tile(sampling_mask[None, None, None, ...],
                             template_shape[:2] + (self.n_channels, 1, 1))
        self.image_vec_mask = np.nonzero(image_mask.flatten())[0]
        self.gradient_mask = np.nonzero(np.tile(
            image_mask[None, ...], (2, 1, 1, 1, 1, 1)))

        template_mask = np.tile(sampling_mask[None, None, None, ...],
                                template_shape[:3] + (1, 1))
        self.template_vec_mask = np.nonzero(template_mask.flatten())[0]

        # Unified -------------------------------------------------------------

        self.eps = eps
        # pre-compute
        self._precompute()

    def _precompute(self):

        # AAM part ------------------------------------------------------------

        # sample appearance model
        self._U = self._U[self.interface.image_vec_mask, :]

        # compute model's gradient
        nabla_t = self.interface.gradient(self.template)

        # compute warp jacobian
        dw_dp = self.interface.dw_dp()

        # set sigma2
        self._sigma2 = self.appearance_model.noise_variance()

        # compute steepest descent images
        j = self.interface.steepest_descent_images(nabla_t, dw_dp)

        # project out appearance model from J
        self._j_aam = j - self._U.dot(self._pinv_U.T.dot(j))

        # compute inverse hessian
        self._h_aam = self._j_aam.T.dot(j)

        # CLM part ------------------------------------------------------------

        # compute warp jacobian
        dw_dp = np.rollaxis(self.transform.d_dp(None), -1)

        # set rho2
        self._rho2 = self.transform.model.noise_variance()

        inv_F = np.real(ifft2(self.multiple_clf.F))[:, None, ...]

        # compute filter gradient
        nabla_F = self.gradient(Image(inv_F))
        nabla_F = np.require(fft2(nabla_F), dtype=np.complex64)

        # compute filter jacobian
        self.j_clm = self.steepest_descent_images(nabla_F, dw_dp)
        h = np.sqrt(np.asarray(self.j_clm.shape[-2]))
        w = h
        self.j_clm = self.j_clm.reshape((self.n_parts, self.n_channels, h, w,
                                         -1))

        # Unified part --------------------------------------------------------

        # set Prior
        sim_prior = np.zeros((4,))
        transform_prior = (self._rho2 * self._sigma2 /
                           self.pdm.model.eigenvalues)
        self._j_prior = np.hstack((sim_prior, transform_prior))
        self._h_prior = np.diag(self._j_prior)

    def run(self, image, initial_shape, gt_shape=None, max_iters=20,
            prior=False):

        # initialize transform
        self.transform.set_target(initial_shape)
        shape_parameters = [self.transform.as_vector()]
        # masked model mean
        masked_m = self.appearance_model.mean().as_vector()[
            self.interface.image_vec_mask]

        parts_template = self.template2.ravel()[self.template_vec_mask]

        for _ in xrange(max_iters):

            # AAM part --------------------------------------------------------

            # compute warped image with current weights
            i = self.interface.warp(image)

            # reconstruct appearance
            masked_i = i.as_vector()[self.interface.image_vec_mask]

            # compute error image
            e_aam = masked_m - masked_i

            # CLM part --------------------------------------------------------

            # warp image
            parts_image = build_parts_image(
                image, self.transform.target, parts_shape=self.parts_shape,
                normalize_parts=self.normalize_parts)

            # compute image response
            parts_fft2 = np.require(fft2(parts_image.pixels[:, 0, ...]),
                                        dtype=np.complex64)
            parts_response = np.sum(self.multiple_clf.F * parts_fft2, axis=-3)
            parts_response[np.logical_not(np.isfinite(parts_response))] = 0.5

            # compute error image
            e_clm = (parts_template -
                     parts_response.ravel()[self.template_vec_mask])

            # compute jacobian response
            j_clm = np.sum(parts_fft2.ravel()[self.image_vec_mask].reshape(
                self.j_clm.shape[:-1])[..., None] * self.j_clm, axis=-4)
            j_clm = j_clm.reshape((-1, self.transform.n_parameters))
            conj_j_clm = np.conj(j_clm)

            # compute hessian
            h_clm = conj_j_clm.T.dot(j_clm)

            # Unified ---------------------------------------------------------

            # compute gauss-newton parameter updates
            if prior:
                h = (self._rho2 * self._h_aam +
                     self._sigma2 * h_clm +
                     self._h_prior)
                b = (self._j_prior * self.transform.as_vector() -
                     self._rho2 * self._j_aam.T.dot(e_aam) -
                     self._sigma2 * conj_j_clm.T.dot(e_clm))
                dp = -np.linalg.solve(h, b)
            else:
                dp = np.linalg.solve(
                    self._h_aam + self._h_clm,
                    self._j_aam.T.dot(e_aam) +
                    self._j_clm.T.dot(e_clm))

            # update transform
            target = self.transform.target
            self.transform.from_vector_inplace(self.transform.as_vector() + dp)
            shape_parameters.append(self.transform.as_vector())

            # test convergence
            error = np.abs(np.linalg.norm(
                target.points - self.transform.target.points))
            if error < self.eps:
                break

        # return dm algorithm result
        return UnifiedAlgorithmResult(image, self, shape_parameters,
                                      gt_shape=gt_shape)

    def gradient(self, image):
        g = fast_gradient(image.pixels.reshape((-1,) + self.parts_shape))
        return g.reshape((2,) + image.pixels.shape)

    def steepest_descent_images(self, gradient, dw_dp):
        # reshape gradient
        # gradient: n_dims x n_parts x offsets x n_ch x (h x w)
        gradient = gradient[self.gradient_mask].reshape(
            gradient.shape[:-2] + (-1,))
        # compute steepest descent images
        # gradient: n_dims x n_parts x offsets x n_ch x (h x w)
        # ds_dp:    n_dims x n_parts x                          x n_params
        # sdi:               n_parts x offsets x n_ch x (h x w) x n_params
        sdi = 0
        a = gradient[..., None] * dw_dp[..., None, None, None, :]
        for d in a:
            sdi += d

        # reshape steepest descent images
        # sdi: (n_parts x n_offsets x n_ch x w x h) x n_params
        return sdi[:, 0, ...]


class AIC_LKInverse(UnifiedAlgorithm):
    r"""
    Alternating Inverse Compositional Algorithm
    """

    def __init__(self, aam_interface, appearance_model, transform,
                 multiple_clf, parts_shape, normalize_parts, covariance, pdm,
                 scale=10, eps=10**-5, **kwargs):

        # AAM part ------------------------------------------------------------

        # set state
        self.appearance_model = appearance_model
        self.template = appearance_model.mean()
        self.transform = transform
        # set interface
        self.interface = aam_interface(self, **kwargs)
        # mask appearance model
        self._U = self.appearance_model.components.T
        self._pinv_U = np.linalg.pinv(
            self._U[self.interface.image_vec_mask, :]).T

        # CLM part ------------------------------------------------------------

        # set state
        self.multiple_clf = multiple_clf
        self.parts_shape = parts_shape
        self.normalize_parts = normalize_parts
        self.covariance = covariance
        self.pdm = pdm
        self.scale = scale

        self.n_parts = self.multiple_clf.F.shape[0]
        self.n_channels = self.multiple_clf.F.shape[-3]

        global multivariate_normal
        if multivariate_normal is None:
            from scipy.stats import multivariate_normal  # expensive

        # compute template response
        mvn = multivariate_normal(mean=np.zeros(2), cov=self.covariance)
        grid = build_sampling_grid(self.parts_shape)
        self.template2 = np.require(
            fft2(np.tile(mvn.pdf(grid)[None, None, None],
                         (self.n_parts, 1, 1, 1, 1))), dtype=np.complex64)

        sampling_mask = kwargs.get('sampling_mask')

        if sampling_mask is None:
            parts_shape = self.parts_shape
            sampling_mask = np.require(np.ones((parts_shape)), dtype=np.bool)

        template_shape = self.template2.shape
        image_mask = np.tile(sampling_mask[None, None, None, ...],
                             template_shape[:2] + (self.n_channels, 1, 1))
        self.image_vec_mask = np.nonzero(image_mask.flatten())[0]
        self.gradient_mask = np.nonzero(np.tile(
            image_mask[None, ...], (2, 1, 1, 1, 1, 1)))

        template_mask = np.tile(sampling_mask[None, None, None, ...],
                                template_shape[:3] + (1, 1))
        self.template_vec_mask = np.nonzero(template_mask.flatten())[0]

        # Unified -------------------------------------------------------------

        self.eps = eps
        # pre-compute
        self._precompute()

    def _precompute(self):

        # AAM part ------------------------------------------------------------

        # compute warp jacobian
        self._dw_dp = self.interface.dw_dp()

        # set sigma2
        self._sigma2 = self.appearance_model.noise_variance()

        # CLM part ------------------------------------------------------------

        # compute warp jacobian
        dw_dp = np.rollaxis(self.pdm.d_dp(None), -1)

        # set rho2
        self._rho2 = self.scale * self.pdm.model.noise_variance()

        inv_F = np.real(ifft2(self.multiple_clf.F))[:, None, ...]

        # compute filter gradient
        nabla_F = self.gradient(Image(inv_F))
        nabla_F = np.require(fft2(nabla_F), dtype=np.complex64)

        # compute filter jacobian
        self.j_clm = self.steepest_descent_images(nabla_F, dw_dp)
        h = np.sqrt(np.asarray(self.j_clm.shape[-2]))
        w = h
        self.j_clm = self.j_clm.reshape((self.n_parts, self.n_channels, h, w,
                                         -1))

        # Unified part --------------------------------------------------------

        # set Prior
        sim_prior = np.zeros((4,))
        transform_prior = (self._rho2 * self._sigma2 /
                           self.pdm.model.eigenvalues)
        self._j_prior = np.hstack((sim_prior, transform_prior))
        self._h_prior = np.diag(self._j_prior)

    def run(self, image, initial_shape, gt_shape=None, max_iters=20,
            prior=False):

        # initialize transform
        self.transform.set_target(initial_shape)
        shape_parameters = [self.transform.as_vector()]
        # initial appearance parameters
        appearance_parameters = [0]
        # model mean
        m = self.appearance_model.mean().as_vector()
        # masked model mean
        masked_m = m[self.interface.image_vec_mask]

        parts_template = self.template2.ravel()[self.template_vec_mask]

        for _ in xrange(max_iters):

            # AAM part --------------------------------------------------------

            # warp image
            i = self.interface.warp(image)
            # mask image
            masked_i = i.as_vector()[self.interface.image_vec_mask]

            # reconstruct appearance
            c = self._pinv_U.T.dot(masked_i - masked_m)
            t = self._U.dot(c) + m
            self.template.from_vector_inplace(t)
            appearance_parameters.append(c)

            # compute error image
            e_aam = (self.template.as_vector()[self.interface.image_vec_mask] -
                     masked_i)

            # compute model gradient
            nabla_t = self.interface.gradient(self.template)

            # compute model jacobian
            j_aam = self.interface.steepest_descent_images(nabla_t,
                                                           self._dw_dp)

            # compute hessian
            h_aam = j_aam.T.dot(j_aam)

            # CLM part --------------------------------------------------------

            # CLM part --------------------------------------------------------

            # warp image
            parts_image = build_parts_image(
                image, self.transform.target, parts_shape=self.parts_shape,
                normalize_parts=self.normalize_parts)

            # compute image response
            parts_fft2 = np.require(fft2(parts_image.pixels[:, 0, ...]),
                                        dtype=np.complex64)
            parts_response = np.sum(self.multiple_clf.F * parts_fft2, axis=-3)
            parts_response[np.logical_not(np.isfinite(parts_response))] = 0.5

            # compute error image
            e_clm = (parts_template -
                     parts_response.ravel()[self.template_vec_mask])

            # compute jacobian response
            j_clm = np.sum(parts_fft2.ravel()[self.image_vec_mask].reshape(
                self.j_clm.shape[:-1])[..., None] * self.j_clm, axis=-4)
            j_clm = j_clm.reshape((-1, self.transform.n_parameters))
            conj_j_clm = np.conj(j_clm)

            # compute hessian
            h_clm = conj_j_clm.T.dot(j_clm)

            # Unified ---------------------------------------------------------

            # compute gauss-newton parameter updates
            if prior:
                h = (self._rho2 * h_aam +
                     self._sigma2 * h_clm +
                     self._h_prior)
                b = (self._j_prior * self.transform.as_vector() -
                     self._rho2 * j_aam.T.dot(e_aam) -
                     self._sigma2 * conj_j_clm.T.dot(e_clm))
                dp = -np.linalg.solve(h, b)
            else:
                dp = np.linalg.solve(
                    self._h_aam + self._h_clm,
                    self._j_aam.T.dot(e_aam) +
                    self._j_clm.T.dot(e_clm))

            # update transform
            target = self.transform.target
            self.transform.from_vector_inplace(self.transform.as_vector() + dp)
            shape_parameters.append(self.transform.as_vector())

            # test convergence
            error = np.abs(np.linalg.norm(
                target.points - self.transform.target.points))
            if error < self.eps:
                break

        # return Unified algorithm result
        return UnifiedAlgorithmResult(
            image, self, shape_parameters,
            appearance_parameters=appearance_parameters, gt_shape=gt_shape)

    def gradient(self, image):
        g = fast_gradient(image.pixels.reshape((-1,) + self.parts_shape))
        return g.reshape((2,) + image.pixels.shape)

    def steepest_descent_images(self, gradient, dw_dp):
        # reshape gradient
        # gradient: n_dims x n_parts x offsets x n_ch x (h x w)
        gradient = gradient[self.gradient_mask].reshape(
            gradient.shape[:-2] + (-1,))
        # compute steepest descent images
        # gradient: n_dims x n_parts x offsets x n_ch x (h x w)
        # ds_dp:    n_dims x n_parts x                          x n_params
        # sdi:               n_parts x offsets x n_ch x (h x w) x n_params
        sdi = 0
        a = gradient[..., None] * dw_dp[..., None, None, None, :]
        for d in a:
            sdi += d

        # reshape steepest descent images
        # sdi: (n_parts x n_offsets x n_ch x w x h) x n_params
        return sdi[:, 0, ...]