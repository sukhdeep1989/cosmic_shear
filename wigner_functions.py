import numpy as np
import torch as tc
from scipy.special import binom,jn,loggamma
from scipy.special import eval_jacobi as jacobi
from multiprocessing import Pool,cpu_count
from torch.multiprocessing import Pool as t_Pool
from functools import partial

def tc_binom(n,k):
    return tc.exp(tc.lgamma(n+1)-tc.lgamma(k+1)-tc.lgamma(n-k+1))

def wigner_d(m1,m2,theta,l):
    k=tc.min(tc.stack((l-m1,l-m2,l+m1,l+m2)),0)[0]
    a=tc.abs(tc.tensor(m1-m2,dtype=l.dtype,device=l.device))
    lamb=0 #lambda
    if m2>m1:
        lamb=m2-m1
    b=2*l-2*k-a
    #d_mat=(-1)**lamb
    d_mat=tc.sqrt(tc_binom(2*l-k,k+a)) #this gives array of shape l with elements choose(2l[i]-k[i], k[i]+a)
    d_mat*=(-1)**lamb
    d_mat/=tc.sqrt(tc_binom(k+b,b))
#     d_mat=tc.atleast_1d(d_mat)
    x=k<0
    d_mat[x]=0
    d_mat=tc.tensor(d_mat,dtype=l.dtype,device=l.device)
    if  d_mat.dim()==0:
        d_mat=tc.tensor([d_mat],dtype=l.dtype,device=l.device)
    d_mat=d_mat.reshape(1,len(d_mat))
    theta=theta.reshape(len(theta),1)
    d_mat=d_mat*((tc.sin(theta/2.0)**a)*(tc.cos(theta/2.0)**b))
    d_mat*=tc.tensor(jacobi(l,a,b,tc.cos(theta)),dtype=l.dtype,device=l.device)
    return d_mat


def wigner_d_parallel(m1,m2,theta,l,ncpu=None):
    if ncpu is None:
        ncpu=cpu_count()
    # p=t_Pool(ncpu)
    with Pool(ncpu) as p: #t_pool
        d_mat=p.map(partial(wigner_d,m1,m2,theta),l)
    # d_mat=tc.stack([d_mat[i][:,0] for i in np.arange(len(l))])
    d_mat=d_mat[:,:,0].T
    #p.close()
    return d_mat

def log_factorial(n):
    return loggamma(n+1)

def Wigner3j_log(j_1, j_2, j_3, m_1, m_2, m_3):
    """Calculate the Wigner 3j symbol `Wigner3j(j_1,j_2,j_3,m_1,m_2,m_3)`

    This function is copied with minor modification from
    sympy.physics.Wigner, as written by Jens Rasch.

    The inputs must be integers.  (Half integer arguments are
    sacrificed so that we can use numba.)  Nonzero return quantities
    only occur when the `j`s obey the triangle inequality (any two
    must add up to be as big as or bigger than the third).

    Examples
    ========

    >>> from spherical_functions import Wigner3j
    >>> Wigner3j(2, 6, 4, 0, 0, 0)
    0.186989398002
    >>> Wigner3j(2, 6, 4, 0, 0, 1)
    0

    """

    #     log_factorial=lambda n: loggamma(n+1)

    if (m_1 + m_2 + m_3 != 0):
        return 0
    if ( (abs(m_1) > j_1) or (abs(m_2) > j_2) or (abs(m_3) > j_3) ):
        return 0
    prefid = (1 if (j_1 - j_2 - m_3) % 2 == 0 else -1)
    m_3 = -m_3
    a1 = j_1 + j_2 - j_3
    a2 = j_1 - j_2 + j_3
    a3 = -j_1 + j_2 + j_3
    if (a1 < 0) or a2<0 or a3<0:
        return 0

    log_argsqrt = ( log_factorial(j_1 + j_2 - j_3) +
                log_factorial(j_1 - j_2 + j_3) +
                log_factorial(-j_1 + j_2 + j_3) +
                log_factorial(j_1 - m_1) +
                log_factorial(j_1 + m_1) +
                log_factorial(j_2 - m_2) +
                log_factorial(j_2 + m_2) +
                log_factorial(j_3 - m_3) +
                log_factorial(j_3 + m_3) ) - log_factorial(j_1 + j_2 + j_3 + 1)

    log_ressqrt=0.5*log_argsqrt

    imin = max(-j_3 + j_1 + m_2, max(-j_3 + j_2 - m_1, 0))
    imax = min(j_2 + m_2, min(j_1 - m_1, j_1 + j_2 - j_3))

    sumres = 0.0
    ii=np.arange(imin, imax + 1)

    log_den = ( log_factorial(ii) +
                log_factorial(ii + j_3 - j_1 - m_2) +
                log_factorial(j_2 + m_2 - ii) +
                log_factorial(j_1 - ii - m_1) +
                log_factorial(ii + j_3 - j_2 + m_1) +
                log_factorial(j_1 + j_2 - j_3 - ii) )
    sgn=np.ones_like(ii) #-1
    sgn[ii % 2 == 1]=-1
    sumres +=np.sum(np.exp(log_ressqrt-log_den)*sgn)  #1.0 / den
    return sumres * prefid #ressqrt taken inside sumres calc
