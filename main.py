import os
import random
import shutil
from time import time

import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np
import torch
import torch_geometric as pyg
from fenics import (XDMFFile, as_backend_type, dx, grad, inner, parameters,
                    plot, set_log_active)
from fenics_adjoint import (Constant, Control, Function, assemble,
                            compute_gradient, interpolate, project, solve)
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import factorized
from scipy.spatial.distance import pdist, squareform

from fem import (build_weakform_filter, build_weakform_struct, epsilon,
                 input_assemble, output_assemble, sigma)
from mesh import (get_clever2d_mesh, get_dof_map, get_mbb2d_mesh,
                  get_wrench2d_mesh, halfcircle2d)
from MMA import mmasub, subsolv
from model import MyGNN, generate_data, pred_input, training
from utils import (calculate_center, compute_theta_error,
                   generate_density_graph, map_density)

set_log_active(False)
if os.path.exists("/workspace/output"):
    shutil.rmtree("/workspace/output")
SAVE_DIR = XDMFFile("output/result.xdmf")
os.mkdir("/workspace/output")
# device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
device = 'cpu'

def main(volfrac, maxiter, N, hmax, hamxC, rmin, Ni, Nf, Wi, Wu, batch_size, epochs, n_hidden, n_layer, lr):
    t_start = time()
    ## time
    t_filter = []
    t_input  = []
    t_fine   = []
    t_coarse = []
    t_append = []
    t_dcdv   = []
    t_training=[]
    t_pred=[]
    t_optimizer=[]
    input_apd=[]
    output_apd=[]

    mesh, V, F, bcs, t, ds, u, du, rho, drho, part_info = get_mbb2d_mesh(hmax=hmax, N=N)
    meshC, VC, FC, bcsC, tC, dsC, uC, duC, _, _, _ = get_mbb2d_mesh(hmax = hmaxC)

    print("fine :", mesh.num_cells(),",","Coarse :", meshC.num_entities(0),",","Patch :", len(part_info['nodes']))

    # v2d, d2v = get_dof_map(F)
    v2dC, d2vC = get_dof_map(FC)
    
    
    center = calculate_center(mesh)

    H = csr_matrix(np.maximum(0, rmin - squareform(pdist(center))))
    hs = H@np.ones(mesh.num_cells())

    # count = np.zeros((len(mesh.coordinates()), 1))  ### num of patches overlab by node
    # for pn in part_info['nodes']:
        # count[pn] += 1

    graph_edge, graph_coords = generate_density_graph(mesh)

    uh = Function(V)
    phih = Function(F)   ## density
    m = Control(phih)
    phih.vector()[:] = volfrac
    dc_pred = Function(F)

    rhoh = Function(F)   ## Filtered density

    ## MMA parameters
    mm = 1
    n = mesh.num_cells()
    xmin = np.zeros((n,1))
    xmax = np.ones((n,1))
    xval = phih.vector()[:][np.newaxis].T
    xold1 = xval.copy()
    xold2 = xval.copy()
    low = np.ones((n,1))
    upp = np.ones((n,1))
    a0 = 1.0
    aa = np.zeros((mm,1))
    c = 10000*np.ones((mm,1))
    d = np.zeros((mm,1))
    move = 0.2


    aH, LH = build_weakform_filter(rho, drho, phih, rmin) #### filter equation
    a, L = build_weakform_struct(u, du, rhoh, t, ds) #### FEA-fine

    uhC = Function(VC)
    rhohC = Function(FC)

    aC, LC = build_weakform_struct(uC, duC, rhohC, tC, dsC) #### FEA-coarse


    loop = 0
    while loop < maxiter:
        tic = time()
        # solve(aH == LH, rhoh) ## density Filtering
        rhoh.assign(phih)
        rhoh.vector()[:] = (H@rhoh.vector()[:])/hs
        t_filter.append(time()-tic)

        tic = time()
        # map_density(rhoh, rhohC, mesh, meshC, v2d, v2dC)
        rhohC = project(rhoh,FC)
        solve(aC == LC, uhC, bcs=bcsC)  ##  Coarse FE
        t_coarse.append(time()-tic)

        tic = time()
        x, scaler = input_assemble(rhoh, uhC, V, F, FC, v2dC, loop, center, scaler if loop > 0 else None)
        # x /= count

        t_input.append(time()-tic)
        x_last = x  
        input_apd.append(x)
                
        if(loop<Ni+Wi) or (divmod(max(loop-Ni-Wi,1),Nf)[1]==0):
            tic = time()
            solve(a == L, uh, bcs=bcs)  ## fine
            t_fine.append(time()-tic)
            Ws = inner(sigma(uh,rhoh), epsilon(uh))
            comp = assemble(Ws*dx)
            vol = assemble(rhoh*dx)
            tic = time()
            dc = compute_gradient(comp, m)   ### fine sensitivity
            dv = compute_gradient(vol, m)

            dc.vector()[:] = (H@dc.vector()[:])/hs
            dv.vector()[:] = (H@dv.vector()[:])/hs

            t_dcdv.append(time()-tic)
            ## Store
            y, scalers, lb = output_assemble(dc, loop, scalers if loop > 0 else None, lb if loop > 0 else None)
            # y /= count

            output_apd.append(y)

            if loop == Ni + Wi -1:
                data_list = []
                tic = time()
                for i in range(Wi):
                    data_list.append([generate_data(input_apd[-(i+1)], output_apd[-(i+1)], node_ids, cell_ids,mesh) for node_ids, cell_ids in zip(part_info['nodes'], part_info['elems'])])
                dataset = sum(data_list,[])
                t_append.append(time()-tic)
                tic = time()
                train_hist, val_hist, net  = training(dataset, batch_size, n_hidden, n_layer, lr, epochs, device)
                t_training.append(time()-tic)
            elif divmod(max(loop-Ni-Wi,1), Nf)[1] == 0:
                data_list = []
                tic = time()
                for i in range(Wu):
                    data_list.append([generate_data(input_apd[-(i+1)], output_apd[-(i+1)], node_ids, cell_ids,mesh) for node_ids, cell_ids in zip(part_info['nodes'], part_info['elems'])])
                dataset = sum(data_list,[])
                t_append.append(time()-tic)
                tic = time()
                train_hist, val_hist, net = training(dataset, batch_size, n_hidden, n_layer, lr, epochs, device, net)
                t_training.append(time()-tic)

            ## MMA parameters
            tic = time()
            mu0 = 1.0
            mu1 = 1.0
            f0val = comp
            df0dx = dc.vector()[:].reshape(-1,1)
            fval = np.array([[phih.vector()[:].sum()/n-volfrac]])
            dfdx = dv.vector()[:].reshape(1,-1)
            xval = phih.vector()[:].reshape(-1,1)
            xmma,ymma,zmma,lam,xsi,eta,mu,zet,s,low,upp = \
                mmasub(mm,n,loop,xval,xmin,xmax,xold1,xold2,f0val,df0dx,fval,dfdx,low,upp,a0,aa,c,d,move)
            xold2 = xold1.copy()
            xold1 = xval.copy()
            phih.vector()[:] = xmma.ravel()
            t_optimizer.append(time()-tic)
        
        else:
            tic = time()
            pred_input_data = [pred_input(x_last, node_ids, cell_ids, mesh) for node_ids, cell_ids in zip(part_info['nodes'], part_info['elems'])]
            pred_loader = pyg.loader.DataLoader(pred_input_data, batch_size = len(pred_input_data))
            with torch.no_grad():
                net.eval()
                for batch in pred_loader:
                    yhat = net(batch.x.to(device), batch.edge_index.to(device)).cpu()
                    # dc_pred.vector()[v2d[batch.global_idx]] = scalers.inverse_transform(
                    #     yhat.numpy()
                    # )[:,0]

                    for i in range(len(batch)):
                        # dc_pred.vector()[v2d[batch[i]].global_idx] += \
                            # yhat.numpy()[batch.batch==i, 0]
                        pass
                        dc_pred.vector()[batch[i].global_idx] += \
                            yhat.numpy()[batch.batch==i, 0]
            # dc_pred.vector()[v2d] = scalers.inverse_transform(dc_pred.vector()[v2d].reshape(-1,1)).ravel()/count.ravel()
            dc_pred.vector()[:] = scalers.inverse_transform(dc_pred.vector()[:].reshape(-1,1)).ravel()
            t_pred.append(time()-tic)

            # therr = compute_theta_error(dc,dc_pred)    ###### theta_error
            # print(f'theta={therr:.3f}')

            tic = time()
            vol = assemble(rhoh*dx)
            dv = compute_gradient(vol, m)
            t_dcdv.append(time()-tic)

            ## MMA parameters
            tic = time()
            mu0 = 1.0
            mu1 = 1.0
            f0val = comp
            df0dx = dc_pred.vector()[:].reshape(-1,1)
            fval = np.array([[phih.vector()[:].sum()/n-volfrac]])
            dfdx = dv.vector()[:].reshape(1,-1)
            xval = phih.vector()[:].reshape(-1,1)
            xmma,ymma,zmma,lam,xsi,eta,mu,zet,s,low,upp = \
                mmasub(mm,n,loop,xval,xmin,xmax,xold1,xold2,f0val,df0dx,fval,dfdx,low,upp,a0,aa,c,d,move)
            xold2 = xold1.copy()
            xold1 = xval.copy()
            phih.vector()[:] = xmma.ravel()
            t_optimizer.append(time()-tic)

        plot(rhoh, cmap="gray_r")
        plt.savefig("test.png")
        loop += 1
        print(f"it.: {loop}", f",obj.: {comp}")
        
    SAVE_DIR.write(rhoh)
    plot(rhoh, cmap = "gray_r")
    # plot(dc)
    # plt.colorbar(plot(dc))
    plt.savefig("test.png")
    print("total time :", time()-t_start)
    print("filter time :", sum(t_filter))
    print("input time :", sum(t_input))
    print("fine time :", sum(t_fine))
    print("coarse time :", sum(t_coarse))
    print("sens time :", sum(t_dcdv))
    print("append time :", sum(t_append))
    print("training time :", sum(t_training))
    print("pred time :", sum(t_pred))
    print("optimizer time :", sum(t_optimizer))


if __name__ == "__main__":
    ## parameters
    volfrac = 0.5
    maxiter = 100
    N = 514    ## number of node in patch
    hmax = 0.025
    hmaxC = 0.03
    rmin = hmax*1.5
    Ni = 1
    Nf = 10
    Wi = 10
    Wu = 5
    batch_size = 32
    epochs = 5
    n_hidden = 512
    n_layer = 5
    lr = 0.005
    torch.manual_seed(42)
    random.seed(42)
    np.random.seed(42)
    main(volfrac, maxiter, N, hmax, hmaxC, rmin, Ni, Nf, Wi, Wu, batch_size, epochs, n_hidden, n_layer, lr)