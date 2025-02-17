#!/usr/bin/env python3
import os, sys, glob
from reeds.modules import do_RE_EDS_sOptimisation as sOptimization

sys.path.append(os.getcwd())

from global_definitions import fM, bash
from global_definitions import name, root_dir
from global_definitions import gromosXX_bin, gromosPP_bin, ene_ana_lib
from global_definitions import in_top_file, in_pert_file, in_disres_file, in_template_reeds_imd


#spefici parts
out_sopt_dir = root_dir+"/TEST_d_sopt_TEST"
next_sopt_dir = root_dir+"/input/2_next_sopt"
in_name = name+"_sopt"

##make folder
out_sopt_dir = bash.make_folder(out_sopt_dir)

#In-Files
topology = fM.Topology(top_path=in_top_file,    disres_path=in_disres_file, perturbation_path=in_pert_file)
coords = glob.glob(next_sopt_dir+"/*cnf")
system =fM.System(coordinates=coords, name=in_name, top=topology)

last_jobID = sOptimization.do(out_root_dir=out_sopt_dir,in_simSystem=system, in_ene_ana_lib_path=ene_ana_lib,verbose=True, trials_per_run=10, duration_per_job = "04:00")


