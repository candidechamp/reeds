import os, glob, warnings
import numpy as np
from collections import OrderedDict
from typing import Union, Dict, List

from pygromos.files import imd, repdat
from pygromos.utils import bash

from reeds.function_libs.analysis import free_energy
from reeds.function_libs.analysis import parameter_optimization
from reeds.function_libs.analysis import sampling as sampling_ana
from reeds.function_libs.optimization import eds_energy_offsets as eds_energy_offsets
from reeds.function_libs.optimization.eds_eoff_rebalancing import rebalance_eoffs_directCounting
from reeds.function_libs.analysis import replica_exchanges as repex

from reeds.function_libs.visualization import pot_energy_plots
from reeds.function_libs.visualization import re_plots as re_plots

from reeds.function_libs.file_management import file_management
from reeds.function_libs.file_management.file_management import parse_csv_energy_trajectories
from reeds.function_libs.utils import s_log_dist as sdist
from reeds.function_libs.utils.structures import adding_Scheme_new_Replicas

from reeds.function_libs.visualization.utils import determine_vrange

template_control_dict = OrderedDict({  # this dictionary is controlling the post  Simulation analysis procedure!
    "concat": {"do": True,
               "sub": {
                   "cp_cnf": True,
                   "cat_trc": True,
                   "cat_tre": False,
                   "ene_ana": True,
                   "convert_trcs": False,
                   "cat_repdat": True, }
               },
    "plot_property_timeseries": {"do": True,
                                 "sub": {
                                    "pot_ene_by_state":False,
                                    "pot_ene_by_replica":False,
                                    "pot_ene_timeseries": False,
                                    "pot_ene_grid_timeseries": True,
                                    "ref_timeseries": True,
                                    "ref_distrib": False,
                                    "distance_restraints": False,
                                    "temperature_2d_plot": False
                                  }
                                 },
    "eoffset": {"do": True,
                "sub": {
                    "eoff_estimation": True,
                    "sampling_plot": True,
                    "eoffset_rebalancing": False,
                }
                },
    "sopt": {"do": True,
             "sub": {
                 "detect_flow_equilib": True,
                 "run_RTO": True,
                 "run_NLRTO": True,
                 "run_NGRTO": False,
                 "visualize_transitions": True,
                 "roundtrips": True,
                 "generate_replica trace": True}
             },
    "phys_sampling": {"do": True},
    "dfmult": {"do": False},
    "compress_simulation_folder": {"do": True},
    "prepare_input_folder": {"do": True,
                             "sub": {
                                 "eoff_to_sopt": False,
                                 "write_eoffRB": False,
                                 "write_eoffEstm": False,
                                 "write_s": True,
                                 "ssm_next_cnf": False
                             },
                             }
})


def dict_to_nice_string(control_dict: Dict) -> str:
    """
        Converts a dictionary of options (like template_control_dict)
	  to a more human readable format. Which can then be printed to a text file,
	  which can be manually modified before submiting analysis jobs.

    Parameters
    ----------
    control_dict : Dict
        analysis control dictonary

    Returns
    -------
    str
        nice formatting of the control dictionary for printing.

    """
    script_text = "control_dict = {\n"
    for key, value in control_dict.items():
        script_text += "\t\"" + key + "\": "
        first = False
        if (type(value) == dict):
            if ("do" in value):  # do should always be first in this list
                script_text += "{\"do\":" + str(value["do"]) + ","
                if (len(value) > 1):
                    script_text += "\n"
                first = True
            for key2, value2 in value.items():  # alternative keys

                # prefix
                if (first):
                    prefix = " "
                    first = False
                else:
                    prefix = "\t\t"

                # key_val
                if (key2 == "do"):
                    continue
                elif (type(value2) == dict):
                    script_text += prefix + "\"" + str(key2) + "\": " + _inline_dict(value2, "\t\t\t") + ",\n"
                else:
                    script_text += prefix + "\"" + str(key2) + "\": " + str(value2) + ","
            script_text += prefix + " },\n"
        else:
            script_text += str(value) + ",\n"
    script_text += "}\n"
    return script_text


def _inline_dict(in_dict: Dict, prefix: str = "\t"):
    """
        translate dictionary to one code line. can be used for meta-scripting

    Parameters
    ----------
    in_dict: Dict
        analysis control dict
    prefix : str, optional
        prfix symbol to dict write out.

    Returns
    -------
    str
        code line.

    """
    msg = "{\n"
    for key, value in in_dict.items():
        if (type(value) == dict):
            msg += prefix + "\"" + str(key) + "\": " + _inline_dict(in_dict=value, prefix=prefix + "\t") + ","
        else:
            msg += prefix + "\"" + str(key) + "\": " + str(value) + ",\n"
    return msg + prefix + "}"


def check_script_control(control_dict: dict = None) -> dict:
    if isinstance(control_dict, type(None)):
        return template_control_dict
    else:
        for x in template_control_dict:
            if x not in control_dict:
                control_dict.update({x: template_control_dict[x]})
    return control_dict

def do_Reeds_analysis(in_folder: str, out_folder: str, gromos_path: str,
                      topology: str, in_ene_ana_lib: str, in_imd: str,
                      optimized_eds_state_folder: str = "../a_optimizedStates/analysis/data",
                      state_undersampling_occurrence_potential_threshold: List[float] = None,
                      state_physical_occurrence_potential_threshold: List[float] = None,
                      undersampling_frac_thresh: float = 0.9,
                      add_s_vals: int = 0, state_weights: List[float]=None, s_opt_trial_range:int=None,
                      adding_new_sReplicas_Scheme: adding_Scheme_new_Replicas = adding_Scheme_new_Replicas.from_below,
                      eoffRebalancing_learningFactor: float = None, eoffRebalancing_pseudocount: float = None,
                      eoffRebalancing_correctionPerReplica: bool = False,
                      grom_file_prefix: str = "test", title_prefix: str = "test", ene_ana_prefix="ey_sx.dat",
                      repdat_prefix: str = "run_repdat.dat",
                      n_processors: int = 1, verbose=False, dfmult_all_replicas=False,
                      ssm_next_cnfs: bool = True,
                      trim_equil:float = 0.1,
                      control_dict: Dict[str, Union[bool, Dict[str, bool]]] = None) -> (
        dict, dict, dict):
    """
         Master calling point from which all jobs can call the analysis functions for a RE-EDS simulation.
	  This function generates: plots, compress files, and/or calculate values of interest.


    Parameters
    ----------
    in_folder : str
        input folder for the simulation.
    out_folder : str
        output folder for the simulation
    gromos_path : str
        gromosPP binary path
    topology : str
        path to topology
    in_ene_ana_lib : str
        in path for ene_ana lib
    in_imd : str
        in path for imd_file
    optimized_eds_state_folder : str, optional
        path to optimized eds_state folders (default: "../a_optimizedState/analysis/data")
    pot_tresh : float, optional
        potential energy treshold (default: 0)
    undersampling_frac_thresh : float, optional
        fraction threshold (default: 0.9)
    take_last_n : int, optional
        this parameter can be used to force the energy offset estimation to use a certain amount of replicas.  (default: None)
    add_s_vals : int, optional
        this parameter can be used to add a number of s-values  during the s-optimization (default: 0)
    state_weights : List[float], optional
        allows to weight the different states in the s-optimization differently (default: None)
    s_opt_trial_range : int, optional
        give a range of trials, that define the start and end of the s-optimization run (default: adding_Scheme_new_Replicas.from_below)
    adding_new_sReplicas_Scheme : int, optional
        how shall the coordinates for new replicas be added to an exchange bottle-neck. (default: adding_Scheme_new_Replicas.from_below)
    grom_file_prefix : str, optional
         provide here a gromos_file prefix of this run (default: test)
    title_prefix : str, optional
        proivde here a output_prefx and plot prefix (default: test)
    ene_ana_prefix : str, optional
        prefix for the ene ana analysis @WARNING: NOT USED ANYMORE! - FUTURE REMOVE!.
    repdat_prefix : str, optional
        prefix for the repdat files. required to read in the repdats. (default:run_repdat.dat )
    n_processors : int, optional
        number of processors
    verbose : bool, optional
        verbosity level
    dfmult_all_replicas : bool, optional
        shall dfmult be calculated for all replicas
    ssm_next_cnfs: bool
        if true, the conformations placed in /analysis/next will be SSM conformations
        if false, the conformations will be the last conformations of the simulation
    trim_equil : float between 0 and 1.
                         corresponds to the fraction of data to remove for equilibration
    control_dict : dict, optional
        control dict for analysis

    Returns
    -------
    (dict, dict, dict)
        eoff_statistic, svals, dFs - the function returns the eoff_statistics,
        the s-values of the s-optimization-results and the free energy calculation results,
        if calculated.

    """

    eoff_statistic = {}
    dFs = {}

    print("Starting RE-EDS analysis:")
    print(f'\tThe first {int(trim_equil*100)} % of the simulation will be discarded for equilibration.')
    print('\tIf you wish to discard more/less, please change the input variable "trim_equil"\n')

    # subfolder for clearer structure
    plot_folder_path = out_folder + "/plots"
    concat_file_folder = bash.make_folder(out_folder + "/data", "-p")

    if (not os.path.exists(out_folder)):
        print("Generating out_folder: ", out_folder)
        bash.make_folder(out_folder)
    if (not os.path.exists(concat_file_folder)):
        bash.make_folder(concat_file_folder)

    # out_files
    repdat_file_out_path = concat_file_folder + "/" + title_prefix + "_" + repdat_prefix
    ene_trajs_prefix = title_prefix + "_energies"

    # manual script control
    control_dict = check_script_control(control_dict)

    # parameter file: <-not needed!
    # if(verbose): print("Reading imd: "+in_imd)
    imd_file = imd.Imd(in_imd)
    s_values = list(map(float, imd_file.REPLICA_EDS.RES))
    eoffs = np.array(list(map(lambda vec: list(map(float, vec)), imd_file.REPLICA_EDS.EIR))).T
    num_states = int(imd_file.REPLICA_EDS.NUMSTATES)

    try:
        if (not isinstance(imd_file.MULTIBATH, type(None))):
            temp = float(imd_file.MULTIBATH.TEMP0[0])
        elif (not isinstance(imd_file.STOCHDYN, type(None))):
            temp = float(imd_file.STOCHDYN.TEMPSD)
        else:
            raise Exception("Either STOCHDYN or MULTIBATH block needs to be defined in imd.")

    except Exception as err:
        print("Failed during analysis\n\t" + "\n\t".join(map(str, err.args)))
        exit(1)

    if (control_dict["concat"]["do"]):
        print("STARTING CONCATENATION.")
        num_replicas = len(s_values)

        # if we're using Stochastic Dynamics, use solutemp2 for ene_ana instead of solvtemp2
        if (isinstance(imd_file.MULTIBATH, type(None)) and not isinstance(imd_file.STOCHDYN, type(None))):
            additional_properties = ("solutemp2", "totdisres")
            boundary_conditions = "v cog"

        # if there's only one bath, use solutemp2 for ene_ana instead of solvtemp2
        elif (not isinstance(imd_file.MULTIBATH, type(None)) and imd_file.MULTIBATH.NBATHS == "1"):
            additional_properties = ("solutemp2", "totdisres")
            boundary_conditions = "r cog"

        else:
            additional_properties = ("solvtemp2", "totdisres")
            boundary_conditions = "r cog"

        out_files = file_management.reeds_project_concatenation(in_folder=in_folder, in_topology_path=topology,
                                                                in_imd=in_imd, num_replicas=num_replicas,
                                                                control_dict=control_dict["concat"]["sub"],
                                                                out_folder=concat_file_folder,
                                                                in_ene_ana_lib_path=in_ene_ana_lib,
                                                                repdat_file_out_path=repdat_file_out_path,
                                                                out_file_prefix=grom_file_prefix, starting_time=0,
                                                                n_processes=n_processors, gromosPP_bin_dir=gromos_path,
                                                                verbose=False,
                                                                additional_properties=additional_properties,
                                                                boundary_conditions=boundary_conditions, 
                                                                s1_only=True)
        if (verbose): print("Done\n")

    # intermezzo generating plots_folder
    if (not os.path.exists(plot_folder_path)):
        plot_folder_path = bash.make_folder(plot_folder_path)

    # Set this to None as a checker to avoid redundant parsing
    energy_trajectories = None

    if (control_dict["plot_property_timeseries"]["do"]):
        sub_control = control_dict["plot_property_timeseries"]["sub"]

        if (verbose): print("\tParse the data:\n")
        
        energy_trajectories = parse_csv_energy_trajectories(concat_file_folder, ene_trajs_prefix, trim_equil=trim_equil)
        v_range = determine_vrange(energy_trajectories, num_states)

        # Plots related to the potential energy distributions of the end states.

        if sub_control["pot_ene_by_state"]:
            if (verbose): print("\n\tPlotting end state potential energy distributions (by state)\n")        
            for state_num in range(1, num_states+1):
                outfile = plot_folder_path + '/' + title_prefix + '_pot_ene_state_' + str(state_num) + '.png'
                pot_energy_plots.plot_energy_distribution_by_state(energy_trajectories, 
                                                                   outfile, 
                                                                   state_num, 
                                                                   s_values,
                                                                   manual_xlim = None, 
                                                                   shared_xaxis = True)
        
        if sub_control["pot_ene_by_replica"]:
            if (verbose): print("\n\tPlotting end state potential energy distributions (by replica)\n")
            for i, ene_traj in enumerate(energy_trajectories):
                outfile =  plot_folder_path + '/' + title_prefix + '_pot_ene_replica_' + str(i+1) + '.png'
                pot_energy_plots.plot_energy_distribution_by_replica(ene_traj, 
                                                                     i+1, 
                                                                     s_values[i],
                                                                     outfile, 
                                                                     manual_xlim = None, 
                                                                     shared_xaxis = True)
        
        # this variable allows to access particular elements in the pandas DataFrame
        singleStates = ['e' + str(i) for i in range(1, num_states+1)]
        
        # Timeseries of the potential energy of the end states.
        for i, ene_traj in enumerate(energy_trajectories):
            if sub_control["pot_ene_timeseries"]:
                out_path = plot_folder_path + "/edsState_potential_timeseries_" + str(ene_traj.s) + ".png"
                pot_energy_plots.plot_potential_timeseries(time=ene_traj.time, 
                                                           potentials=ene_traj[singleStates],
                                                           y_range=v_range, 
                                                           title="EDS_stateV_scatter",
                                                           out_path=out_path)
             
            if sub_control["pot_ene_grid_timeseries"]:
                out_path = plot_folder_path + '/' + title_prefix + '_pot_ene_timeseries_' + str(i+1) + '.png'
                title = title_prefix + ' potential energy timeseries - s = ' + str(s_values[i])
                pot_energy_plots.plot_sampling_grid(traj_data = ene_traj, 
                                                    y_range= v_range, 
                                                    out_path=out_path,
                                                    title=title, 
                                                    sampling_thresholds = state_physical_occurrence_potential_threshold,
                                                    undersampling_thresholds = state_undersampling_occurrence_potential_threshold)

        # Plots related to the reference potential energy (V_R)

        if sub_control["ref_timeseries"]:
            outfile = plot_folder_path + '/' + title_prefix + '_ref_pot_ene_timeseries.png'
            pot_energy_plots.plot_ref_pot_ene_timeseries(energy_trajectories, outfile, s_values)

        if sub_control["ref_distrib"]:
            outfile = plot_folder_path + '/' + title_prefix + '_ref_pot_ene_distrib.png'
            pot_energy_plots.plot_ref_pot_energy_distribution(energy_trajectories, outfile, s_values)
            
        if (sub_control["distance_restraints"]):
            if (verbose): print("\tPLOT Disres_bias timeseries:\n")
            for ene_traj in energy_trajectories:
                # plot disres_contrib:
                out_path = plot_folder_path + "/distance_restraints_" + str(ene_traj.s) + ".png"
                singleStates = ["totdisres"]

                pot_energy_plots.plot_potential_timeseries(time=ene_traj["time"], 
                                                           potentials=ene_traj[singleStates],
                                                           title="EDS disres Potential s" + str(ene_traj.s), 
                                                           y_label="E/[kj/mol]",
                                                           x_label="t/[ps]",
                                                           out_path=out_path)

        if (sub_control["temperature_2d_plot"]):
            print("\tPLOT temperature 2D histogram:\t")

            if (isinstance(imd_file.MULTIBATH, type(None)) and not isinstance(imd_file.STOCHDYN, type(None))):
                pot_energy_plots.plot_replicaEnsemble_property_2D(ene_trajs=energy_trajectories,
                                                                  out_path=plot_folder_path + "/temperature_heatMap.png",
                                                                  temperature_property="solutemp2")

            elif (not isinstance(imd_file.MULTIBATH, type(None)) and imd_file.MULTIBATH.NBATHS == 1):
                pot_energy_plots.plot_replicaEnsemble_property_2D(ene_trajs=energy_trajectories,
                                                                  out_path=plot_folder_path + "/temperature_heatMap.png",
                                                                  temperature_property="solutemp2")
                
            else:
                pot_energy_plots.plot_replicaEnsemble_property_2D(ene_trajs=energy_trajectories,
                                                                  out_path=plot_folder_path + "/temperature_heatMap.png",
                                                                  temperature_property="solvtemp2")

            if (verbose): print("DONE\n")

    if (control_dict["phys_sampling"]["do"] and not state_physical_occurrence_potential_threshold is None):
        # parsing_ene_traj_csvs 
        if energy_trajectories is None:
            energy_trajectories = parse_csv_energy_trajectories(concat_file_folder, ene_trajs_prefix, trim_equil=trim_equil)
     
        out_dir = bash.make_folder(out_folder + "/state_sampling")

        (sampling_results, out_dir) = sampling_ana.sampling_analysis(out_path=out_dir,
                                                                     ene_trajs=energy_trajectories,
                                                                     eoffs=eoffs,
                                                                     s_values=s_values,
                                                                     state_potential_treshold=state_physical_occurrence_potential_threshold)
    elif(control_dict["phys_sampling"]["do"]):
        warnings.warn("DID NOT do physical sampling analysis, as state_physical_occurrence_potential_threshold was None!")


    if (control_dict["eoffset"]["do"]):
        print("Start Eoffset")
        sub_control = control_dict["eoffset"]["sub"]
        out_dir = bash.make_folder(out_folder + "/eoff")

        # parsing_ene_traj_csvs 
        if energy_trajectories is None:
            energy_trajectories = parse_csv_energy_trajectories(concat_file_folder, ene_trajs_prefix, trim_equil=trim_equil)

        if (not os.path.exists(concat_file_folder)):
            raise IOError("could not find needed energies (contains all ene ana .dats) folder in:\n " + out_folder)
        
        # plot if states are sampled and minimal state
        (sampling_results, out_dir) = sampling_ana.detect_undersampling(out_path = out_dir, 
                                                                        ene_trajs = energy_trajectories,
                                                                        _visualize=sub_control["sampling_plot"], 
                                                                        s_values = s_values, eoffs=eoffs, 
                                                                        state_potential_treshold= state_undersampling_occurrence_potential_threshold, 
                                                                        undersampling_occurence_sampling_tresh=undersampling_frac_thresh)
                                                                        
        if(sub_control["eoff_estimation"] and sub_control["eoffset_rebalancing"]):
            raise Exception("can not have eoff_estimation and eoff Rebalancing turned on at the same time!")

        elif (sub_control["eoff_estimation"]):
            print("calc Eoff: ")
            # WARNING ASSUMPTION THAT ALL EOFF VECTORS ARE THE SAME!
            print("\tEoffs(" + str(len(eoffs[0])) + "): ", eoffs[0])
            print("\tS_values(" + str(len(s_values)) + "): ", s_values)
            print("\tsytsemTemp: ", temp)

            # Decrement the value of undersampling_idx by 1. As indexing followed a different convention. 
            new_eoffs_estm, all_eoffs = eds_energy_offsets.estimate_energy_offsets(ene_trajs = energy_trajectories, initial_offsets = eoffs[0], sampling_stat=sampling_results, s_values = s_values,
                                                                                   out_path = out_dir, temp = temp, undersampling_idx = sampling_results['undersamplingThreshold']-1,
                                                                                   plot_results = True, calc_clara = False)
            print("ENERGY OFFSETS ESTIMATION:\n") 
            print("new_eoffs_estm: " + str(np.round(new_eoffs_estm, 2)))
        elif(sub_control["eoffset_rebalancing"]):
            new_eoffs_rb = rebalance_eoffs_directCounting(sampling_stat=sampling_results['samplingDistributions'], old_eoffs=eoffs,
                                                       learningFactor=eoffRebalancing_learningFactor, pseudo_count=eoffRebalancing_pseudocount,
                                                       correct_for_s1_only=not eoffRebalancing_correctionPerReplica)
            new_eoffs_rb = new_eoffs_rb.T
            
        if (verbose): print("Done\n")
    
    new_svals = None

    if (control_dict["sopt"]["do"]):
        print ('\nANALYSIS of the S-DISTRIBUTION')
        sub_control = control_dict["sopt"]["sub"]
        out_dir = bash.make_folder(out_folder + "/s_optimization")

        # get repdat file
        in_file = glob.glob(repdat_file_out_path)[0]
        print("Found repdat file: " + str(in_file))
        
        # Calculate the exchanges for this re-eds simulation
        # Repdat is read here once and passed to all subfuncions.
        # Similarly transitions are calculated here so its only done once.

        exchange_data = repdat.Repdat(in_file, trim_equil=trim_equil)
        exchange_freq = repex.calculate_exchange_freq(exchange_data)
        transitions = exchange_data.get_replica_traces() 

        if (sub_control["run_RTO"]):
            new_svals = parameter_optimization.optimize_s(repdat=exchange_data, 
                                                          out_dir=out_dir,
                                                          title_prefix="s_opt", 
                                                          in_imd=in_imd,
                                                          add_s_vals=add_s_vals, 
                                                          trial_range=s_opt_trial_range,
                                                          state_weights=state_weights,
                                                          run_NLRTO=sub_control["run_NLRTO"], 
                                                          run_NGRTO=sub_control["run_NGRTO"],
                                                          verbose=verbose
                                                         )

        if (sub_control["visualize_transitions"]):
            print("\t\tvisualize transitions")
            parameter_optimization.get_s_optimization_transitions(out_dir = out_dir, 
                                                                  repdat = exchange_data,
                                                                  transitions = transitions,
                                                                  title_prefix=title_prefix, 
                                                                  undersampling_thresholds = state_undersampling_occurrence_potential_threshold
                                                                 )
            
            re_plots.plot_exchange_freq(s_values, exchange_freq, outfile = out_dir + '/exchange_frequencies.png')            

        if (sub_control["roundtrips"] and sub_control["run_RTO"]):
            print("\t\tshow roundtrips")

            # plot
            if verbose: print("Plotting Histogramm")
            re_plots.plot_repPos_replica_histogramm(out_path=out_dir + "/replica_repex_pos.png", 
                                                    data=transitions, title=title_prefix, s_values=s_values)

        if (verbose): print("Done\n")
    
    if (control_dict["dfmult"]["do"]):
        print("Start Dfmult")

        # check convergence:
        dfmult_convergence_folder = out_folder + "/free_energy"
        if (not os.path.isdir(dfmult_convergence_folder)):
            bash.make_folder(dfmult_convergence_folder, "-p")

        if energy_trajectories is None:
            energy_trajectories = parse_csv_energy_trajectories(concat_file_folder, ene_trajs_prefix, trim_equil=trim_equil)

        free_energy.calc_free_energies_with_mbar(energy_trajectories, s_values, eoffs, dfmult_convergence_folder, temp, num_replicas=len(energy_trajectories))

        free_energy.free_energy_convergence_analysis(ene_trajs=energy_trajectories, 
                                                     out_dir=dfmult_convergence_folder,
                                                     out_prefix=title_prefix, 
                                                     in_prefix=ene_trajs_prefix, 
                                                     verbose=verbose,
                                                     dfmult_all_replicas=dfmult_all_replicas)

        

    # When we reach here, we no longer need the data in energy_trajectories, memory can be freed.
    del energy_trajectories

    if (control_dict["prepare_input_folder"]["do"]):
        sub_control = control_dict["prepare_input_folder"]["sub"]
        print("PREPARE NEXT FOLDER - for following simulation")

        next_dir = bash.make_folder(out_folder + "/next", "-p")
        next_imd = next_dir + "/next.imd"
        
        if (sub_control["eoff_to_sopt"]):
            new_svals = sdist.generate_preoptimized_sdist(s_values, num_states, exchange_freq, s_values[sampling_results['undersamplingThreshold']+2])

        # add new cnf s for the new S-distribution
        print("Place the proper conformations (.cnf) files in analysis/next")

        # possible sets of conformations to provide for next simulation

        sort_cnfs = lambda x: int(x.split("_")[-1].split(".")[0])

        final_cnfs = sorted(glob.glob(concat_file_folder+"/*.cnf"), key=sort_cnfs)
        opt_state_cnfs = sorted(glob.glob(optimized_eds_state_folder+'/*.cnf'), key=sort_cnfs)

        #
        # Put the proper cnfs in place
        # 

        if (sub_control["eoff_to_sopt"]):
            if (not os.path.isdir(optimized_eds_state_folder)):
                raise IOError("Could not find optimized state output dir: " + optimized_eds_state_folder)
        
        if new_svals is None:
            new_svals = s_values

        if len(new_svals) <= len(s_values) or sub_control["eoff_to_sopt"]:
            if sub_control["ssm_next_cnf"] or sub_control["eoff_to_sopt"]:
                for i in range(len(new_svals)):
                    bash.copy_file(opt_state_cnfs[(i)%num_states], f'{next_dir}/{title_prefix}_{i+1}.cnf')
            else:
                # call code to place final cnfs
                for i, cnf in enumerate(final_cnfs):
                    bash.copy_file(cnf, f'{next_dir}/{title_prefix}_{i+1}.cnf')
       
        # When we have more replicas, we need to add coordinates (takes cnf from closest neighbour)
        elif len(s_values) < len(new_svals):
            if control_dict["sopt"]["sub"]["run_NGRTO"]:
                warnings.warn("The function add_cnf_sopt_LRTOlike() was not meant to be used with GRTO + addition of replicas.\nThe code will run and provide .cnf files but probably not trustworthy ones.")
            
            file_management.add_cnf_sopt_LRTOlike(in_dir=concat_file_folder, out_dir=next_dir, in_old_svals=s_values,
                                                  cnf_prefix=title_prefix,
                                                  in_new_svals=new_svals, replica_add_scheme=adding_new_sReplicas_Scheme,
                                                  verbose=verbose)

        # write next_imd.
        print("Writing out the next .imd file ")
        imd_file = imd.Imd(in_imd)

        ##New EnergyOffsets
        if(sub_control["write_eoffRB"] and sub_control["write_eoffEstm"]):
            raise Exception("can not write eoffRB and eoffEstm in new imd!")
        elif sub_control["write_eoffRB"] and control_dict["eoffset"]["do"] and control_dict['eoffset']['sub']['eoffset_rebalancing']:
            imd_file.edit_REEDS(EIR=np.round(new_eoffs_rb, 2))
        elif sub_control["write_eoffEstm"] and control_dict["eoffset"]["do"] and control_dict['eoffset']['sub']['eoff_estimation']:
            imd_file.edit_REEDS(EIR=np.round(new_eoffs_estm, 2))
        elif ((sub_control["write_eoffRB"] or sub_control["write_eoffEstm"]) and not control_dict["Eoff"]["do"] and not (control_dict['eoffset']['sub']['eoff_estimation'] or control_dict['eoffset']['sub']['eoffset_rebalancing'])):
            raise Exception("can not write eoffRB or eoffEstm as no eoff step chosen active!")

        ##New S-Values?=
        if (sub_control["write_s"] and control_dict["sopt"]["sub"]["run_RTO"]) or sub_control["eoff_to_sopt"]:
            imd_file.edit_REEDS(SVALS=new_svals)
        elif (sub_control["write_s"] and not control_dict["sopt"]["sub"]["run_RTO"]):
            warnings.warn("Could not set s-values to imd, as not calculated in this run!")

        imd_file.write(next_imd)
        if (verbose): print("Done\n")

    if (control_dict["compress_simulation_folder"]["do"]):
        print("Compress simulation folder")
        in_tre = sorted(glob.glob(concat_file_folder + "/*.tre"))
        in_trc = sorted(glob.glob(concat_file_folder + "/*.trc"))
        compress_files = in_tre + in_trc
        compress_list = [in_folder]

        file_management.compress_files(in_paths=compress_files)
        file_management.compress_folder(in_paths=compress_list)
        if (verbose): print("Done\n")

    return eoff_statistic, new_svals, dFs
