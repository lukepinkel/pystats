#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Nov  5 20:08:47 2020

@author: lukepinkel
"""


import numpy as np 
import scipy as sp
import scipy.stats 
from ..utilities.linalg_operations import (_check_shape, vec, invec, vech, invech)
from ..utilities.special_mats import dmat, lmat, nmat, kmat



class SEM:
    
    def __init__(self, Lambda, Beta, Phi, Psi, data=None, S=None, indicator_vars=None):
        if S is None:
            S = data.cov()
        Lambda = Lambda.loc[S.index] #Align loadings and variable order
        
        n_mvars, n_lvars = Lambda.shape 
        
        #If covariance is free, then an indicator variable needs to be set
        #Defaults to the first variable that loads onto the each latent var 
        
        LA = np.asarray(Lambda)
        BE = np.asarray(Beta)
        PH = np.asarray(Phi)
        PS = np.asarray(Psi)
        
        LAf = Lambda.copy() 
        
        if indicator_vars is None:
            for i, var in enumerate(Lambda.columns):
                if Phi.loc[var, var]==1.0:
                    vi = np.argmax(LAf.loc[:, var])
                    LAf.iloc[vi, i] = 0.0
        else:
            LAf = LAf - indicator_vars
        
        LAf = np.asarray(LAf)
        BEf = np.asarray(Beta.copy())
        PSf = np.asarray(Psi.copy())
        PHf = np.asarray(Phi.copy())
        
        theta_indices = np.concatenate([vec(LAf), vec(BEf),  vech(PHf), 
                                       vech(PSf)])!=0
        
        params_template = np.concatenate([vec(LA), vec(BE), 
                                          vech(PH), vech(PS)])
        theta = params_template[theta_indices]
        
        param_parts = np.cumsum([0, n_mvars*n_lvars,
                       n_lvars*n_lvars,
                       (n_lvars+1)*n_lvars//2,
                       (n_mvars+1)*n_mvars//2])
        slices = dict(LA=np.s_[param_parts[0]:param_parts[1]],
                      BE=np.s_[param_parts[1]:param_parts[2]],
                      PH=np.s_[param_parts[2]:param_parts[3]],
                      PS=np.s_[param_parts[3]:param_parts[4]])
        mat_shapes = dict(LA=(n_mvars, n_lvars),
                          BE=(n_lvars, n_lvars),
                          PH=(n_lvars, n_lvars),
                          PS=(n_mvars, n_mvars))
        self.LA, self.BE, self.PH, self.PS, self.S = LA, BE, PH, PS, np.asarray(S)
        self.theta_indices = theta_indices
        self.params_template = params_template
        self.theta = theta
        self.param_parts = param_parts
        self.slices = slices
        self.mat_shapes = mat_shapes
        self.n_mvars, self.n_lvars=  n_mvars, n_lvars
        self.n_params = len(params_template)
        self.I_nlvars = np.eye(n_lvars)
        self.Lp = lmat(self.n_mvars).A
        self.Np = nmat(self.n_mvars).A
        self.Ip = np.eye(self.n_mvars)
        self.Dk = dmat(self.n_lvars).A
        self.Kq = kmat(self.n_lvars, self.n_lvars).A
        self.Kp = kmat(self.n_mvars, self.n_lvars).A
        self.Kkp = kmat(self.n_lvars, self.n_mvars).A
        self.Dp = dmat(self.n_mvars).A
        self.E = np.zeros((self.n_mvars, self.n_mvars))
        self.Ip2 = np.eye(self.n_mvars**2)
        self.DPsi = np.linalg.multi_dot([self.Lp, self.Ip2, self.Dp])        
        self.LpNp = np.dot(self.Lp, self.Np)
        self.bounds = np.concatenate([vec(np.zeros(self.LA.shape)), 
                                      vec(np.zeros(self.BE.shape)),
                                     vech(np.eye(self.PH.shape[0])),
                                     vech(np.eye(self.PS.shape[0]))])
        self.bounds = self.bounds[self.theta_indices]
        self.bounds = [(None, None) if x==0 else (0, None) for x in self.bounds]
        self.n_obs = data.shape[0]
        self.data = data
        
    def model_matrices(self, theta):
        theta = _check_shape(theta, 1)
        params = self.params_template.copy()
        if theta.dtype==complex:
            params = params.astype(complex)
        params[self.theta_indices] = theta
        LA = invec(params[self.slices['LA']], *self.mat_shapes['LA'])
        BE = invec(params[self.slices['BE']], *self.mat_shapes['BE'])
        IB = np.linalg.pinv(self.I_nlvars - BE)
        PH = invech(params[self.slices['PH']])
        PS = invech(params[self.slices['PS']])
        return LA, BE, IB, PH, PS
    
    def implied_cov(self, theta):
        LA, _, IB, PH, PS = self.model_matrices(theta)
        A = LA.dot(IB)
        Sigma = A.dot(PH).dot(A.T) + PS
        return Sigma
    
    def _dsigma(self, LA, BE, IB, PH, PS):
        A = np.dot(LA, IB)
        B = np.linalg.multi_dot([A, PH, IB.T])
        DLambda = np.dot(self.LpNp, np.kron(B, self.Ip))
        DBeta = np.dot(self.LpNp, np.kron(B, A))
        DPhi = np.linalg.multi_dot([self.Lp, np.kron(A, A), self.Dk])
        DPsi = self.DPsi        
        G = np.block([DLambda, DBeta, DPhi, DPsi])
        return G
    
    def dsigma(self, theta):
        LA, BE, IB, PH, PS = self.model_matrices(theta)
        return self._dsigma(LA, BE, IB, PH, PS)
        
    
    def _gradient(self, theta):
        LA, BE, IB, PH, PS = self.model_matrices(theta)
        A = LA.dot(IB)
        Sigma = A.dot(PH).dot(A.T) + PS
        Sigma_inv = np.linalg.pinv(Sigma)
        G = self.dsigma(theta)
        W = 0.5 * self.Dp.T.dot(np.kron(Sigma_inv, Sigma_inv)).dot(self.Dp)
        d = vech(self.S - Sigma)[:, None]
        g = -2.0 * G.T.dot(W).dot(d)
        return g
    
    def gradient(self, theta):
        return self._gradient(theta)[self.theta_indices, 0]
    
    def _hessian(self, theta):
        LA, BE, IB, PH, PS = self.model_matrices(theta)
        A = LA.dot(IB)
        Sigma = A.dot(PH).dot(A.T) + PS
        Sigma_inv = np.linalg.pinv(Sigma)
        Sdiff = self.S - Sigma
        d = vech(Sdiff)
        G = self.dsigma(theta)
        DGp = self.Dp.dot(G)
        W1 = np.kron(Sigma_inv, Sigma_inv)
        W2 = np.kron(Sigma_inv, Sigma_inv.dot(Sdiff).dot(Sigma_inv))
        H1 = 0.5 * DGp.T.dot(W1).dot(DGp)
        H2 = 1.0 * DGp.T.dot(W2).dot(DGp)

        Hpp = []
        U, A = IB.dot(PH).dot(IB.T), LA.dot(IB)
        Q = LA.dot(U)
        Kp, Kq, D, Dp, E = self.Kkp, self.Kq, self.Dk, self.Dp, self.E
        Hij = np.zeros((self.n_params, self.n_params))
        for i in range(self.n_mvars):
            for j in range(i, self.n_mvars):
                E[i, j] = 1.0
                T = E + E.T
                TA = T.dot(A)
                AtTQ = A.T.dot(T).dot(Q)
                AtTA = A.T.dot(TA)
                
                H11 = np.kron(U, T)
                H22 = np.kron(AtTQ.T, IB.T).dot(Kq)+Kq.dot(np.kron(AtTQ, IB))\
                      +np.kron(U, AtTA)
                H12 = (np.kron(U, TA)) + Kp.dot(np.kron(T.dot(Q), IB))
                H13 =  np.kron(IB, TA).dot(D) 
                H23 = D.T.dot(np.kron(IB.T, AtTA))
                
                
                Hij[self.slices['LA'], self.slices['LA']] = H11
                Hij[self.slices['BE'], self.slices['BE']] = H22
                Hij[self.slices['LA'], self.slices['BE']] = H12
                Hij[self.slices['LA'], self.slices['PH']] = H13
                Hij[self.slices['PH'], self.slices['BE']] = H23
                Hij[self.slices['BE'], self.slices['LA']] = H12.T
                Hij[self.slices['PH'], self.slices['LA']] = H13.T
                Hij[self.slices['BE'], self.slices['PH']] = H23.T  
                E[i, j] = 0.0
                Hpp.append(Hij[:, :, None])
                Hij = Hij*0.0
        W = np.linalg.multi_dot([Dp.T, W1, Dp])
        dW = np.dot(d, W)
        Hp = np.concatenate(Hpp, axis=2) 
        H3 = np.einsum('k,ijk ->ij', dW, Hp)      
        H = (H1 + H2 - H3 / 2.0)*2.0
        return H
    
    def hessian(self, theta):
        return self._hessian(theta)[self.theta_indices][:, self.theta_indices]
    
    def loglike(self, theta):
        LA, _, IB, PH, PS = self.model_matrices(theta)
        A = LA.dot(IB)
        Sigma = A.dot(PH).dot(A.T) + PS
        Sigma_inv = np.linalg.pinv(Sigma)
        LL = np.linalg.slogdet(Sigma)[1] + np.trace(self.S.dot(Sigma_inv))
        return LL
    
    def fit(self):
        theta = self.theta.copy()
        opt = sp.optimize.minimize(self.loglike, theta, jac=self.gradient,
                                   hess=self.hessian, method='trust-constr',
                                   bounds=self.bounds)
        theta = opt.x
        self.opt = opt
        self.theta = theta
        self.Acov = (np.linalg.inv(self.hessian(theta)) * self.n_obs)
                

        
        