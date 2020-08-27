# import numpy as np
# import cupy as cp

import sigpy as sp
from sigpy import util

class TotalVariationRecon(sp.app.LinearLeastSquares):
    r"""Total variation regularized reconstruction.

     Considers the problem:

     .. math::
         \min_x \frac{1}{2} \| P F S x - y \|_2^2 + \lambda \| G_t x \|_1

     where P is the sampling operator, F is the Non uniform Fourier transform operator,
     S is the SENSE operator, G is the gradient operator,
     x is the image, and y is the k-space measurements.

     Args:
         ksp (array): k-space measurements.
         dcf (float or array): weights for non-Cartesian Sampling.
         traj (None or array): coordinates.
         mps (array): sensitivity maps.
         reg_lambda (float): temporal TV regularization parameter.

         device (Device): device to perform reconstruction.
         # coil_batch_size (int): batch size to process coils.
         Only affects memory usage.
         comm (Communicator): communicator for distributed computing.
         **kwargs: Other optional arguments.

     References:
         Block, K. T., Uecker, M., & Frahm, J. (2007).
         Undersampled radial MRI with multiple coils.
         Iterative image reconstruction using a total variation constraint.
         Magnetic Resonance in Medicine, 57(6), 1086-1098.

    Modified from TotalVariationRecon in Sigpy package to apply TV along time dimension
    Implemented by Yongwan Lim (yongwanl@usc.edu) Feb. 2020
    """

    def __init__(self, ksp, dcf, traj, mps, reg_lambda, dim_fd=(0,),
                 device=sp.cpu_device, comm=None, show_pbar=True, **kwargs):

        ksp = sp.to_device(ksp * (dcf**0.5), device=device)

        (n_ch, n_y, n_x) = mps.shape

        S = sp.linop.Multiply((1, n_y, n_x), mps)
        R = sp.linop.Reshape([1] + list(ksp.shape[1:]), ksp.shape[1:])

        T = []
        for ksp_each, traj_each in zip(ksp, traj):
            F = sp.linop.NUFFT((n_ch, n_x, n_y), traj_each)
            P = sp.linop.Multiply(ksp_each.shape, dcf**0.5)

            T.append(R * P * F * S)

        A = sp.linop.Diag(T, iaxis=0, oaxis=0)
        G = sp.linop.FiniteDifference(A.ishape, axes=dim_fd)
        proxg = sp.prox.L1Reg(G.oshape, reg_lambda)

        def g(x):
            device = sp.get_device(x)
            xp = device.xp
            with device:
                # TODO: can it be numerically beneficial to add the eps?
                return reg_lambda * xp.sum(xp.sqrt(xp.abs(x)**2 + xp.finfo(float).eps)).item()

        if comm is not None:
            show_pbar = show_pbar and comm.rank == 0

        super().__init__(A, ksp, proxg=proxg, g=g, G=G, show_pbar=show_pbar, **kwargs)


class TotalVariationReconNLCG:
    """constrained reconstruction of the problem:
    min_x ||Ax-y||_2^2 + lambda_t*||delta_t x||_1

    :param kdata: k-space data
    :param kweight: density compensation function
    :param kloc: k-space trajectory
    :param sens_map: sensitivity map
    :param lambda_t: regularization parameter
    :param max_iter: maxmium iteration number
    :return: img: reconstructed image
    """

    def __init__(self, ksp, dcf, traj, mps, lambda_t, max_iter, step_size=2, device=sp.cpu_device):

        ksp = sp.to_device(ksp * (dcf ** 0.5), device=device)
        (n_ch, n_y, n_x) = mps.shape

        S = sp.linop.Multiply((1, n_y, n_x), mps)
        R = sp.linop.Reshape([1] + list(ksp.shape[1:]), ksp.shape[1:])

        T = []
        for ksp_each, traj_each in zip(ksp, traj):
            F = sp.linop.NUFFT((n_ch, n_x, n_y), traj_each)
            P = sp.linop.Multiply(ksp_each.shape, dcf ** 0.5)

            T.append(R * P * F * S)

        A = sp.linop.Diag(T, iaxis=0, oaxis=0)
        G = sp.linop.FiniteDifference(A.ishape, axes=(0,))

        self.device = device

        self.A = A
        self.y = ksp
        self.G = G
        with self.device:
            self.x = self.A.H(ksp)

        self.init_step_size = step_size
        self.max_iter = max_iter
        self.lambda_t = lambda_t

    def _update_fidelity(self, img):
        with self.device:
            r = self.y - self.A(img)
            return self.A.H(r)

    def _update_temporal_fd(self, img):
        with self.device:
            xp = self.device.xp
            temp_a = xp.diff(img, n=1, axis=0)
            temp_a = temp_a / xp.sqrt(abs(temp_a) ** 2 + xp.finfo(float).eps)
            temp_b = xp.diff(temp_a, n=1, axis=0)
            ttv_update = xp.zeros(img.shape, dtype=xp.complex64)
            ttv_update[0, :, :] = temp_a[0, :, :]
            ttv_update[1:-1, :, :] = temp_b
            ttv_update[-1, :, :] = -temp_a[-1, :, :]

            return ttv_update
            # return self.G.H(self.G(img))

    def _calculate_fnorm(self, img):
        with self.device:
            xp = self.device.xp
            r = self.y - self.A(img)
            return xp.real(xp.vdot(r, r)) / img.size

    def _calculate_tnorm(self, img):
        with self.device:
            xp = self.device.xp
            dtimg = xp.diff(img, n=1, axis=0)
            return self.lambda_t * xp.sum(xp.abs(dtimg)) / img.size
            # return self.lambda_t * xp.sum(xp.abs(self.G(img))) / img.size

    def run(self):
        with self.device:
            xp = self.device.xp
            img = self.x
            step_size = self.init_step_size

            fnorm = []
            tnorm = []
            cost = []
            for iter in range(self.max_iter):
                # calculate gradient of fidelity and regularization
                f_new = self._update_fidelity(img)
                util.axpy(f_new, self.lambda_t, xp.squeeze(self._update_temporal_fd(img)))

                f2_new = xp.vdot(f_new, f_new)

                if iter == 0:
                    f2_old = f2_new
                    f_old = f_new

                # conjugate gradient
                beta = f2_new / (f2_old + xp.finfo(float).eps)
                util.axpy(f_new, beta, f_old)
                f2_old = f2_new
                f_old = f_new

                # update image
                fnorm_t = self._calculate_fnorm(img)
                tnorm_t = self._calculate_tnorm(img)
                cost_t = fnorm_t+tnorm_t

                step_size = self._line_search(img, f_new, cost_t, step_size)
                util.axpy(img, step_size, f_old)

                #  TODO stop criteria
                # if abs(np.vdot(update_old.flatten(), update_old.flatten())) * step_size < 1e-6:
                #    break
                if step_size < 2e-3:
                    break

                fnorm.append(fnorm_t)
                tnorm.append(tnorm_t)
                cost.append(cost_t)

                print("Iter[%d/%d]\tStep:%.5f\tCost:%.3f" % (iter+1, self.max_iter, step_size, cost_t))

            return img, fnorm, tnorm, cost

    def _line_search(self, img, f_new, cost_old, step_size, max_iter=15, a=1.3, b=0.8):
        with self.device:
            flag = False

            for i in range(max_iter):
                img_new = img + step_size * f_new
                fnorm = self._calculate_fnorm(img_new)
                tnorm = self._calculate_tnorm(img_new)
                cost_new = fnorm + tnorm

                if cost_new > cost_old and flag is False:
                    step_size = step_size * b
                elif cost_new < cost_old:
                    step_size = step_size * a
                    cost_old = cost_new
                    flag = True
                elif cost_new > cost_old and flag is True:
                    step_size = step_size / a
                    break

            return step_size


def TotalVariationRecon_NLCG(kdata, kweight, kloc, sens_map, lambda_t, max_iter):
    """constrained reconstruction of the problem:
    min_x ||Ax-y||_2^2 + lambda_t*||delta_t x||_1

    :param kdata: k-space data
    :param kweight: density compensation function
    :param kloc: k-space trajectory
    :param sens_map: sensitivity map
    :param lambda_t: regularization parameter
    :param max_iter: maxmium iteration number
    :return: img: reconstructed image
    """
    device = sp.get_device(kdata)
    xp = device.xp

    nc = sens_map.shape[0]
    rNy = sens_map.shape[1]
    rNx = sens_map.shape[2]
    nframe = kdata.shape[0]
    img = xp.zeros([nframe, nc, rNy, rNx], dtype=xp.complex64)

    for iframe in range(0, nframe):
        img[iframe, :, :, :] = sp.nufft_adjoint(kdata[iframe, :, :, :] * kweight,
                                                kloc[iframe, :, :, :], (nc, rNy, rNx))

    img = img * xp.conj(sens_map)
    img = xp.sum(img, 1)

    step_size = 2
    cost = xp.zeros([1, max_iter])
    fnorm = xp.zeros([1, max_iter])
    tnorm = xp.zeros([1, max_iter])

    for iter in range(0, max_iter):
        # calculate gradient of fidelity and regularization
        update_term, fnorm[0, iter] = fidelity_update(img, kdata, kweight, kloc, sens_map)
        update_term = update_term + lambda_t * temporal_tv_update(img)
        if iter == 0:
            update_old = update_term

        # conjugate gradient
        beta = xp.vdot(update_term.flatten(), update_term.flatten()) / \
               (xp.vdot(update_old.flatten(), update_old.flatten()) + xp.finfo(float).eps)
        update_term = update_term + beta * update_old
        update_old = update_term

        # update image
        tnorm[0, iter] = calculate_tnorm(img, lambda_t)
        cost[0, iter] = fnorm[0, iter] + tnorm[0, iter]
        step_size = line_search(img, update_term, kdata, kweight, kloc, sens_map, step_size, cost[0, iter], lambda_t)
        img = img + step_size * update_old

        #  TODO stop criteria
        # if abs(np.vdot(update_old.flatten(), update_old.flatten())) * step_size < 1e-6:
        #    break
        if step_size < 1e-4:
            break

        print(iter)
        print(step_size)
        print(cost[0, iter])

    return img, fnorm[0, 0:iter + 1], tnorm[0, 0:iter + 1], cost[0, 0:iter + 1]


def temporal_tv_update(img):
    device = sp.get_device(img)
    xp = device.xp

    temp_a = xp.diff(img, n=1, axis=0)
    temp_b = temp_a / xp.sqrt(abs(temp_a) ** 2 + xp.finfo(float).eps)
    temp_c = xp.diff(temp_b, n=1, axis=0)
    ttv_update = xp.zeros(img.shape, dtype=xp.complex64)
    ttv_update[0, :, :] = temp_b[0, :, :]
    ttv_update[1:-1, :, :] = temp_c
    ttv_update[-1, :, :] = -temp_b[-1, :, :]

    return ttv_update


def fidelity_update(img, kdata, kweight, kloc, sens_map):
    device = sp.get_device(img)
    xp = device.xp

    nframe = img.shape[0]
    rNy = img.shape[1]
    rNx = img.shape[2]
    nc = sens_map.shape[0]
    update_term = xp.zeros([nframe, nc, rNy, rNx], dtype=xp.complex64)
    fidelity_norm = 0
    for iframe in range(0, nframe):
        update_term[iframe, :, :, :] = sens_map * img[iframe, :, :]
        update_kspace = sp.nufft(update_term[iframe, :, :],
                                 kloc[iframe, :, :, :])
        update_kspace = kdata[iframe, :, :, :] - update_kspace
        fidelity_norm = fidelity_norm + xp.vdot(update_kspace.flatten(), update_kspace.flatten())
        update_term[iframe, :, :, :] = sp.nufft_adjoint(update_kspace * kweight,
                                                        kloc[iframe, :, :, :], (nc, rNy, rNx))
    update_term = update_term * xp.conj(sens_map)
    update_term = xp.sum(update_term, 1)

    return update_term, abs(fidelity_norm) / img.size


def line_search(img, update_term, kdata, kweight, kloc, sens_map, step_size, cost_old, lambda_t):
    max_iter = 15
    a = 1.3
    b = 0.8
    flag = 0
    for i in range(0, max_iter):
        img_new = img + step_size * update_term
        _, fnorm = fidelity_update(img_new, kdata, kweight, kloc, sens_map)
        tnorm = calculate_tnorm(img_new, lambda_t)
        cost_new = fnorm + tnorm
        if cost_new > cost_old and flag == 0:
            step_size = step_size * b
        elif cost_new < cost_old:
            step_size = step_size * a
            cost_old = cost_new
            flag = 1
        elif cost_new > cost_old and flag == 1:
            step_size = step_size / a
            break

    return step_size


def calculate_tnorm(img, lambda_t):
    device = sp.get_device(img)
    xp = device.xp
    dtimg = xp.diff(img, n=1, axis=0)
    dtimg = abs(dtimg)
    dtimg = sum(dtimg.flatten())
    dtimg = lambda_t * dtimg
    return dtimg / img.size
