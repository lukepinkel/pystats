# -*- coding: utf-8 -*-
"""
Created on Fri Mar  5 21:02:07 2021

@author: lukepinkel
"""
import re
import patsy
import numpy as np
import scipy as sp
import scipy.stats
import scipy.interpolate
import matplotlib.pyplot as plt
from ..pyglm.families import Gaussian
from ..utilities.splines import (_get_crsplines, _get_bsplines, _get_ccsplines,
                                 crspline_basis, bspline_basis, ccspline_basis,
                                 get_penalty_scale, absorb_constraints)
def parse_smooths(smoother_formula, data):
    smooths = {}
    smooth_terms = re.findall("(?<=s[(])(.*?)(?=[)])", smoother_formula)
    arg_order = ['x', 'df', 'kind', 'by']
    for term in smooth_terms:
        tokens = [t.strip() for t in term.split(',')]
        smooth_info = dict(x=None, df=10, kind='cr', by=None)
        for i, token in enumerate(tokens):
            if token.find('=')!=-1:
                key, val = token.split('=')
                smooth_info[key.strip()] = val.strip()
            else:
                key, val = arg_order[i], token.strip()
                smooth_info[key] = val
        smooth_info['df'] = int(smooth_info['df'])
        smooth_info['kind'] = smooth_info['kind'].replace("'", "")
        var = smooth_info['x']
        smooth_info['x'] = data[smooth_info['x']].values
        smooth_info['kind'] = smooth_info['kind'].replace("'", "")
        if smooth_info['by'] is not None:
            smooth_info['by'] = patsy.dmatrix(f"C({smooth_info['by']})-1", data, 
                                              return_type='dataframe',
                                              eval_env=0).values
        smooths[var] = smooth_info
    return smooths

def get_parametric_formula(formula):
    tmp = re.findall("s[(].*?[)]", formula)
    frm = formula[:-1]+formula[-1:]
    for x in tmp:
        frm = frm.replace(x, "")
    frm = re.sub("(\+|\-)(\+|\-)+", replace_duplicate_operators, frm)
    frm = re.sub("\+$", "", frm)
    return frm

def replace_duplicate_operators(match):
    return match.group()[-1:]

def get_smooth(x, df=10, kind="cr", by=None):
    methods = {"cr":_get_crsplines, "cc":_get_bsplines, "bs":_get_ccsplines}
    X, S, knots, fkws = methods[kind](x, df)
    sc = get_penalty_scale(X, S)
    q, _ = np.linalg.qr(X.mean(axis=0).reshape(-1, 1), mode='complete')
    X, S = absorb_constraints(q, X=X, S=S)
    S = S / sc
    smooth_list = []
    if by is not None:
        for i in range(by.shape[1]):
            Xi = X * by[:, [i]]
            x0 = x*by[:,i]
            smooth_list.append(dict(X=Xi, S=S, knots=knots, kind=kind, 
                                        q=q, sc=sc, fkws=fkws, x0=x0,
                                        xm=x0[x0!=0]))
    else:
        smooth_list = [dict(X=X, S=S, knots=knots, kind=kind, q=q, sc=sc, 
                            fkws=fkws, x0=x, xm=None)]
    return smooth_list

class GaussianAdditiveModel:
    
    def __init__(self, formula, data, family=None):
        if family is None:
            family = Gaussian()
        smooth_info = parse_smooths(formula, data)
        formula = get_parametric_formula(formula)
        y, Xp = patsy.dmatrices(formula, data, return_type='dataframe', 
                                eval_env=1)
        varnames = Xp.columns.tolist()
        smooths = {}
        start = p = Xp.shape[1]
        ns = 0
        for key, val in smooth_info.items():
            slist = get_smooth(**val)
            if len(slist)==1:
                smooths[key], = slist
                p_i = smooths[key]['X'].shape[1]
                varnames += [f"{key}{j}" for j in range(1, p_i+1)]
                p += p_i
                ns += 1
            else:
                for i, x in enumerate(slist):
                    by_key = f"{key}{i+1}"
                    smooths[by_key] = slist[i]
                    p_i = smooths[by_key]['X'].shape[1]
                    varnames += [f"{by_key}_{j}" for j in range(1, p_i+1)]
                    p += p_i
                    ns += 1
        X, S, Sj, ranks, ldS = [Xp], np.zeros((ns, p, p)), [], [], []
        for i, (var, s) in enumerate(smooths.items()):
            p_i = s['X'].shape[1]
            Si, ix = np.zeros((p, p)), np.arange(start, start+p_i)
            start += p_i
            Si[ix, ix.reshape(-1, 1)] = s['S']
            smooths[var]['ix'], smooths[var]['Si'] = ix, Si
            X.append(smooths[var]['X'])
            S[i] = Si
            Sj.append(s['S'])
            ranks.append(np.linalg.matrix_rank(Si))
            u = np.linalg.eigvals(s['S'])
            ldS.append(np.log(u[u>np.finfo(float).eps]).sum())
        self.X, self.Xp, self.y = np.concatenate(X, axis=1), Xp.values, y.values[:, 0]
        self.S, self.Sj, self.ranks, self.ldS = S, Sj, ranks, ldS
        self.f, self.smooths = family, smooths
        self.ns, self.n_obs, self.nx = ns, self.X.shape[0], self.X.shape[1]
        self.mp = self.nx - np.sum(self.ranks)
        self.data = data
        theta = np.zeros(self.ns+1)
        for i, (var, s) in enumerate(smooths.items()):
            ix = smooths[var]['ix']
            a = self.S[i][ix, ix[:, None].T]
            d = np.diag(self.X[:, ix].T.dot(self.X[:, ix]))
            lam = (1.5 * (d / a)[a>0]).mean()
            theta[i] = np.log(lam)
        theta[-1] = 1.0
        self.theta = theta
        self.varnames = varnames
        self.smooth_info = smooth_info
        
    def get_wz(self, eta):
        mu = self.f.inv_link(eta)
        v = self.f.var_func(mu=mu)
        dg = self.f.dinv_link(eta)
        r = self.y - mu
        a = 1.0 + r * (self.f.dvar_dmu(mu) / v + self.f.d2link(mu) * dg)
        z = eta + r / (dg * a)
        w = a * dg**2 / v
        return z, w
    
    def solve_pls(self, eta, S):
        z, w = self.get_wz(eta)
        Xw = self.X * w[:, None]
        beta_new = np.linalg.solve(Xw.T.dot(self.X)+S, Xw.T.dot(z))
        return beta_new
        
    def pirls(self, alpha, n_iters=200, tol=1e-7):
        beta = np.zeros(self.X.shape[1])
        S = self.get_penalty_mat(alpha)
        eta = self.X.dot(beta)
        dev = self.f.deviance(self.y, mu=self.f.inv_link(eta)).sum()
        for i in range(n_iters):
            beta_new = self.solve_pls(eta, S)
            eta_new = self.X.dot(beta_new)
            dev_new = self.f.deviance(self.y, mu=self.f.inv_link(eta_new)).sum()
            if dev_new > dev:
                success=False
                break
            if abs(dev - dev_new) / dev_new < tol:
                success = True
                break
            eta = eta_new
            dev = dev_new
            beta = beta_new
        return beta, eta, dev, success, i

    def get_penalty_mat(self, alpha):
        Sa = np.einsum('i,ijk->jk', alpha, self.S)
        return Sa
    
    def logdetS(self, alpha, phi):
        logdet = 0.0
        for i, (r, lds) in enumerate(list(zip(self.ranks, self.ldS))):
            logdet += r * np.log(alpha[i]/phi) + lds
        return logdet
    
    def grad_beta_rho(self, beta, alpha):
        S = self.get_penalty_mat(alpha)
        A = np.linalg.inv(self.hess_dev_beta(beta, S))
        dbdr = np.zeros((beta.shape[0], alpha.shape[0]))
        for i in range(self.ns):
            Si = self.S[i]
            dbdr[:, i] = -alpha[i] * A.dot(Si.dot(beta))*2.0
        return dbdr
    
    def hess_dev_beta(self, beta, S):
        mu = self.f.inv_link(self.X.dot(beta))
        v0, g1 = self.f.var_func(mu=mu), self.f.dlink(mu)
        v1, g2 = self.f.dvar_dmu(mu), self.f.d2link(mu)
        r = self.y - mu
        w = (1.0 + r * (v1 / v0 + g2 / g1)) / (v0 * g1**2)
        d2Ddb2 = 2.0 * (self.X * w[:, None]).T.dot(self.X) + 2.0 * S
        return d2Ddb2
    
    def reml(self, theta):
        lam, phi = np.exp(theta[:-1]), np.exp(theta[-1])
        S, X, y = self.get_penalty_mat(lam), self.X, self.y
        beta = np.linalg.solve(X.T.dot(X) + S, X.T.dot(y))
        r = y - X.dot(beta)
        rss = r.T.dot(r)
        bsb = beta.T.dot(S).dot(beta)
        ldh = np.linalg.slogdet(X.T.dot(X) / phi + S / phi)[1]
        lds = self.logdetS(lam, phi)
        Dp = (rss + bsb) / phi
        K = ldh - lds
        ls = (self.n_obs) * np.log(2.0*np.pi*phi)
        L = (Dp + K + ls) / 2.0
        return L
    
    def gradient(self, theta):
        lam, phi = np.exp(theta[:-1]), np.exp(theta[-1])
        S, X, y = self.get_penalty_mat(lam), self.X, self.y
        beta = np.linalg.solve(X.T.dot(X) + S, X.T.dot(y))
        g = np.zeros_like(theta)
        for i in range(self.ns):
            Si = self.S[i]
            ai = lam[i]
            dbsb = beta.T.dot(Si).dot(beta) * ai / (phi)
            dldh = np.trace(np.linalg.pinv(X.T.dot(X)+S).dot(Si)) * ai
            dlds = self.ranks[i]
            g[i] = dbsb + dldh - dlds
        
        r = y - X.dot(beta)
        rss = r.T.dot(r)
        bsb = beta.T.dot(S).dot(beta)
        g[-1] = -(rss + bsb) / (phi) + self.n_obs - self.mp
        g /= 2.0
        return g
    
    def hessian(self, theta):
        lam, phi = np.exp(theta[:-1]), np.exp(theta[-1])
        S, X, y = self.get_penalty_mat(lam), self.X, self.y
        beta = np.linalg.solve(X.T.dot(X) + S, X.T.dot(y))
        V = X.T.dot(X) + S
        A = np.linalg.inv(V)
        db = self.grad_beta_rho(beta, lam)

        H = np.zeros((self.ns+1, self.ns+1))
        for i in range(self.ns):
            for j in range(i, self.ns):
                Sib = self.S[i].dot(beta)
                t1 = (i==j) * np.dot(beta.T, Sib) * lam[i] / (2*phi)
                t2 = lam[i] / (2*phi) * (db[:, j].T.dot(Sib) + Sib.T.dot(db[:, j]))
                t3 = -lam[i]*lam[j]/2.0 * np.trace(A.dot(self.S[i]).dot(A).dot(self.S[j]))
                t4 = (i==j) * lam[i] / 2.0 * np.trace(A.dot(self.S[i]))
                #t5 = -1.0 / phi * db[:, i].T.dot(V).dot(db[:, j])
                H[i, j] = H[j, i] = t1+t2+t3+t4#+t5
                H[-1, j] -= t1
                H[j, -1] -= t1
        r = y - X.dot(beta)
        rss = r.T.dot(r)
        bsb = beta.T.dot(S).dot(beta)
        H[-1, -1] = (rss + bsb) / (phi*2.0)
        return H
    
    
    def get_smooth_comps(self, beta, ci=90):
        methods = {"cr":crspline_basis, "cc":ccspline_basis,"bs":bspline_basis} 
        f = {}
        ci = sp.stats.norm(0, 1).ppf(1.0 - (100 - ci) / 200)
        for i, (key, s) in enumerate(self.smooths.items()):
            knots = s['knots']         
            x = np.linspace( knots.min(),  knots.max(), 200)
            X = methods[s['kind']](x, knots, **s['fkws'])
            X, _ = absorb_constraints(s['q'], X=X)
            y = X.dot(beta[s['ix']])
            ix = s['ix'].copy()[:, None]
            Vc = self.Vc[ix, ix.T]
            se = np.sqrt(np.diag(X.dot(Vc).dot(X.T))) * ci
            f[key] = np.vstack((x, y, se)).T
        return f
            
    def plot_smooth_comp(self, beta, single_fig=True, subplot_map=None, 
                         ci=95, fig_kws={}):
        ci = sp.stats.norm(0, 1).ppf(1.0 - (100 - ci) / 200)
        methods = {"cr":crspline_basis, "cc":ccspline_basis,"bs":bspline_basis} 
        if single_fig:
            fig, ax = plt.subplots(**fig_kws)
            
        if subplot_map is None:
            subplot_map = dict(zip(np.arange(self.ns), np.arange(self.ns)))
        for i, (key, s) in enumerate(self.smooths.items()):
            knots = s['knots']         
            x = np.linspace( knots.min(),  knots.max(), 200)
            X = methods[s['kind']](x, knots, **s['fkws'])
            X, _ = absorb_constraints(s['q'], X=X)
            y = X.dot(beta[s['ix']])
            ix = s['ix'].copy()[:, None]
            Vc = self.Vc[ix, ix.T]
            se = np.sqrt(np.diag(X.dot(Vc).dot(X.T))) * ci
            if not single_fig: 
                fig, ax = plt.subplots()
                ax.plot(x, y)
                ax.fill_between(x, y-se, y+se, color='b', alpha=0.4)
            else:
                ax[subplot_map[i]].plot(x, y)
                ax[subplot_map[i]].fill_between(x, y-se, y+se, color='b', alpha=0.4)
        return fig, ax
    
    def optimize_penalty(self, opt_kws={}):
        x = self.theta.copy()
        opt = sp.optimize.minimize(self.reml, x, jac=self.gradient, 
                                   hess=self.hessian, method='trust-constr',
                                   **opt_kws)
        theta = opt.x.copy()
        rho, logscale = theta[:-1], theta[-1]
        lambda_, scale = np.exp(rho), np.exp(logscale)
        beta, eta, dev, _, _ = self.pirls(lambda_)
        X, Slambda = self.X, self.get_penalty_mat(lambda_)
        XtX = X.T.dot(X)
        Vb = np.linalg.inv(XtX + Slambda) * scale
        Vp = np.linalg.inv(self.hessian(theta))
        Jb = self.grad_beta_rho(beta, lambda_)
        C = Jb.dot(Vp[:-1, :-1]).dot(Jb.T)
        Vc = Vb + C
        Vf = Vb.dot(XtX/scale).dot(Vb) + C
        self.Slambda = Slambda
        self.Vb, self.Vp, self.Vc, self.Vf = Vb, Vp, Vc, Vf
        self.opt, self.theta, self.scale = opt, theta, scale
        self.beta, self.eta, self.dev = beta, eta, dev
    
    
