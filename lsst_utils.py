from scipy.stats import norm as gaussian
import copy
import numpy as np
from lensing_utils import *
from astropy.cosmology import Planck15 as cosmo
cosmo_h_PL=cosmo.clone(H0=100)


def lsst_pz_source(alpha=1.24,z0=0.51,beta=1.01,z=[]):
    p_zs=z**alpha*np.exp(-(z/z0)**beta)
    p_zs/=np.sum(np.gradient(z)*p_zs)
    return p_zs

def ztrue_given_pz_Gaussian(zp=[],p_zp=[],bias=[],sigma=[],zs=None,ns=0):
    """
        zp: photometrix redshift bins
        p_zp: Probability p(zp) of galaxies in photometric bins
        bias: bias in the true redshift distribution relative to zp.. gaussian will be centered at 
                pz+bias
        sigma: scatter in the true redshift distribution relative to zp.. sigma error of the 
                gaussian
        zs: True z_source, for which to compute p(z_source)
        ns: Source number density. Needed to return n(zs)
    """
    if zs is None:
        zs=np.linspace(min(zp-sigma*5),max(zp-sigma*5),500)
    
    y=np.tile(zs, (len(zp),1)  )
    pdf=gaussian.pdf(y.T,loc=zp+bias,scale=sigma).T
    # pdf=np.zeros((len(zp),len(zs)))
    # for i in np.arange(len(zp)):
    #     pdf[i,:]=gaussian.pdf(zs,loc=zp[i]+bias[i],scale=sigma[i])
    
    dzp=np.gradient(zp)
    
    p_zs=np.dot(dzp*p_zp,pdf)

    dzs=np.gradient(zs)
    p_zs/=np.sum(p_zs*dzs)
    nz=dzs*p_zs*ns
    return zs,p_zs,nz
        
def source_tomo_bins(zp=None,p_zp=None,nz_bins=None,ns=26,ztrue_func=None,zp_bias=None,
                    zp_sigma=None,zs=None):
    """
        Setting source redshift bins in the format used in code. 
        Need 
        zs (array): redshift bins for every source bin. if z_bins is none, then dictionary with 
                    with values for each bin
        p_zs: redshift distribution. same format as zs
        z_bins: if zs and p_zs are for whole survey, then bins to divide the sample. If 
                tomography is based on lens redshift, then this arrays contains those redshifts.
        ns: The number density for each bin to compute shape noise.
    """
    zs_bins={}

    if nz_bins is None:
        nz_bins=1
    z_bins=np.linspace(min(zp)-0.0001,max(zp)+0.0001,nz_bins+1)

    if zs is None:
        zs=np.linspace(0,2.5,100)
    dzs=np.gradient(zs)
    dzp=np.gradient(zp) if len(zp)>1 else [1]
    zp=np.array(zp)

    zl_kernel=np.linspace(0,2,50)
    lu=Lensing_utils()
    cosmo_h=cosmo_h_PL

    for i in np.arange(nz_bins):
        zs_bins[i]={}
        indx=zp.searchsorted(z_bins[i:i+2])
        
        if ztrue_func is None:
            if indx[0]==indx[1]:
                indx[1]=-1
            zs=zp[indx[0]:indx[1]]
            p_zs=p_zp[indx[0]:indx[1]]
            nz=ns*p_zs*dzp[indx[0]:indx[1]]
            print(indx,zs,z_bins)
        else:
            ns_i=ns*np.sum(p_zp[indx[0]:indx[1]]*dzp[indx[0]:indx[1]])
            zs,p_zs,nz=ztrue_func(zp=zp[indx[0]:indx[1]],p_zp=p_zp[indx[0]:indx[1]],
                            bias=zp_bias[indx[0]:indx[1]],
                            sigma=zp_sigma[indx[0]:indx[1]],zs=zs,ns=ns_i)
        x= p_zs>1.e-10
        zs_bins[i]['z']=zs[x]
        zs_bins[i]['dz']=np.gradient(zs_bins[i]['z']) if len(zs_bins[i]['z'])>1 else 1
        zs_bins[i]['nz']=nz[x]
        zs_bins[i]['W']=1.
        zs_bins[i]['pz']=p_zs[x]*zs_bins[i]['W']
        zs_bins[i]['pzdz']=zs_bins[i]['pz']*zs_bins[i]['dz']
        zs_bins[i]['Norm']=np.sum(zs_bins[i]['pzdz'])
        sc=1./lu.sigma_crit(zl=zl_kernel,zs=zs[x],cosmo_h=cosmo_h)
        zs_bins[i]['lens_kernel']=np.dot(zs_bins[i]['pzdz'],sc)
    zs_bins['n_bins']=nz_bins #easy to remember the counts
    zs_bins['z_lens_kernel']=zl_kernel
    return zs_bins



def lens_wt_tomo_bins(zp=None,p_zp=None,nz_bins=None,ns=26,ztrue_func=None,zp_bias=None,
                        zp_sigma=None,cosmo_h=None,z_bins=None):
    """
        Setting source redshift bins in the format used in code. 
        Need 
        zs (array): redshift bins for every source bin. if z_bins is none, then dictionary with 
                    with values for each bin
        p_zs: redshift distribution. same format as zs
        z_bins: if zs and p_zs are for whole survey, then bins to divide the sample. If 
                tomography is based on lens redshift, then this arrays contains those redshifts.
        ns: The number density for each bin to compute shape noise.
    """
    if nz_bins is None:
        nz_bins=1

    z_bins=np.linspace(min(zp),max(zp)*0.9,nz_bins) if z_bins is None else z_bins

    zs_bins0=source_tomo_bins(zp=zp,p_zp=p_zp,zp_bias=zp_bias,zp_sigma=zp_sigma,ns=ns,nz_bins=1)
    lu=Lensing_utils()

    if cosmo_h is None:
        cosmo_h=cosmo_h_PL

    zs=zs_bins0[0]['z']
    p_zs=zs_bins0[0]['z']
    dzs=zs_bins0[0]['dz']
    zs_bins={}

    zl=np.linspace(0,2,50)
    sc=1./lu.sigma_crit(zl=zl,zs=zs,cosmo_h=cosmo_h)
    scW=1./np.sum(sc,axis=1)

    for i in np.arange(nz_bins):
        i=np.int(i)
        zs_bins[i]=copy.deepcopy(zs_bins0[0])
        zs_bins[i]['W']=1./lu.sigma_crit(zl=z_bins[i],zs=zs,cosmo_h=cosmo_h)
        zs_bins[i]['W']*=scW
        zs_bins[i]['pz']*=zs_bins[i]['W']
        
        x= zs_bins[i]['pz']>-1 #1.e-10 # FIXME: for shape noise we check equality of 2 z arrays. Thats leads to null shape noise when cross the bins in covariance
        for v in ['z','pz','dz','W','nz']:
            zs_bins[i][v]=zs_bins[i][v][x]
        
        zs_bins[i]['pzdz']=zs_bins[i]['pz']*zs_bins[i]['dz']
        zs_bins[i]['Norm']=np.sum(zs_bins[i]['pzdz'])
        zs_bins[i]['pz']/=zs_bins[i]['Norm']
        zs_bins[i]['lens_kernel']=np.dot(zs_bins[i]['pzdz'],sc)/zs_bins[i]['Norm']
    zs_bins['n_bins']=nz_bins #easy to remember the counts
    zs_bins['z_lens_kernel']=zl
    zs_bins['z_bins']=z_bins
    return zs_bins

