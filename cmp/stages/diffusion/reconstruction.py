# Copyright (C) 2009-2017, Ecole Polytechnique Federale de Lausanne (EPFL) and
# Hospital Center and University of Lausanne (UNIL-CHUV), Switzerland
# All rights reserved.
#
#  This software is distributed under the open-source license Modified BSD.

""" Reconstruction methods and workflows
"""

# General imports
import re
import os
import shutil
from traits.api import *
from traitsui.api import *
import pkg_resources

import nipype.pipeline.engine as pe
import nipype.interfaces.utility as util
import nipype.interfaces.diffusion_toolkit as dtk
import nipype.interfaces.fsl as fsl
import nipype.interfaces.freesurfer as fs
import nipype.interfaces.mrtrix as mrtrix
import nipype.interfaces.camino as camino
from nipype.utils.filemanip import split_filename

from nipype.interfaces.base import CommandLine, CommandLineInputSpec,\
    traits, TraitedSpec, BaseInterface, BaseInterfaceInputSpec
import nipype.interfaces.base as nibase

from cmp.interfaces.mrtrix3 import Erode, MRtrix_mul, MRThreshold, MRConvert, EstimateResponseForSH, ConstrainedSphericalDeconvolution, DWI2Tensor, Tensor2Vector
from nipype.interfaces.mrtrix3.reconst import FitTensor, EstimateFOD
from nipype.interfaces.mrtrix3.utils import TensorMetrics
# from nipype.interfaces.mrtrix3.preprocess import ResponseSD
from cmp.interfaces.misc import flipBvec
from cmp.interfaces.dipy import DTIEstimateResponseSH, CSD, SHORE
# from nipype.interfaces.dipy import CSD

import cmp.interfaces.diffusion_toolkit as cmp_dtk

from nipype import logging
iflogger = logging.getLogger('nipype.interface')

# Reconstruction configuration

class DTK_recon_config(HasTraits):
    imaging_model = Str
    maximum_b_value = Int(1000)
    gradient_table_file = Enum('siemens_06',['mgh_dti_006','mgh_dti_018','mgh_dti_030','mgh_dti_042','mgh_dti_060','mgh_dti_072','mgh_dti_090','mgh_dti_120','mgh_dti_144',
                          'siemens_06','siemens_12','siemens_20','siemens_30','siemens_64','siemens_256','Custom...'])
    gradient_table = Str
    custom_gradient_table = File
    flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))
    dsi_number_of_directions = Enum([514,257,124])
    number_of_directions = Int(514)
    number_of_output_directions = Int(181)
    recon_matrix_file = Str('DSI_matrix_515x181.dat')
    apply_gradient_orientation_correction = Bool(True)
    number_of_averages = Int(1)
    multiple_high_b_values = Bool(False)
    number_of_b0_volumes = Int(1)

    compute_additional_maps = List(['gFA','skewness','kurtosis','P0'],
                                  editor=CheckListEditor(values=['gFA','skewness','kurtosis','P0'],cols=4))

    traits_view = View(Item('maximum_b_value',visible_when='imaging_model=="DTI"'),
                       Item('gradient_table_file',visible_when='imaging_model!="DSI"'),
                       Item('dsi_number_of_directions',visible_when='imaging_model=="DSI"'),
                       Item('number_of_directions',visible_when='imaging_model!="DSI"',enabled_when='gradient_table_file=="Custom..."'),
                       Item('custom_gradient_table',visible_when='imaging_model!="DSI"',enabled_when='gradient_table_file=="Custom..."'),
                       Item('flip_table_axis',style='custom',label='Flip table:'),
                       Item('number_of_averages',visible_when='imaging_model=="DTI"'),
                       Item('multiple_high_b_values',visible_when='imaging_model=="DTI"'),
                       'number_of_b0_volumes',
                       Item('apply_gradient_orientation_correction',visible_when='imaging_model!="DSI"'),
                       Item('compute_additional_maps',style='custom',visible_when='imaging_model!="DTI"'),
                       )

    def _dsi_number_of_directions_changed(self, new):
        print("Number of directions changed to %d" % new )
        self.recon_matrix_file = 'DSI_matrix_%(n_directions)dx181.dat' % {'n_directions':int(new)+1}

    def _gradient_table_file_changed(self, new):
        if new != 'Custom...':
            self.gradient_table = os.path.join(pkg_resources.resource_filename('cmtklib',os.path.join('data','diffusion','gradient_tables')),new+'.txt')
            if os.path.exists('cmtklib'):
                self.gradient_table = os.path.abspath(self.gradient_table)
            self.number_of_directions = int(re.search('\d+',new).group(0))

    def _custom_gradient_table_changed(self, new):
        self.gradient_table = new

    def _imaging_model_changed(self, new):
        if new == 'DTI' or new == 'HARDI':
            self._gradient_table_file_changed(self.gradient_table_file)

class Dipy_recon_config(HasTraits):
    imaging_model = Str
    # flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))
    flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))
    # gradient_table = File
    local_model_editor = Dict({False:'1:Tensor',True:'2:Constrained Spherical Deconvolution'})
    local_model = Bool(True)
    lmax_order = Enum(['Auto',2,4,6,8,10,12,14,16])
    # normalize_to_B0 = Bool(False)
    single_fib_thr = Float(0.7,min=0,max=1)
    recon_mode = Str

    mapmri = Bool(False)

    tracking_processing_tool = Enum('MRtrix','Dipy')

    laplacian_regularization = traits.Bool(True, usedefault=True, desc = ('Apply laplacian regularization'))

    laplacian_weighting= traits.Float(0.05, usedefault=True, desc = ('Regularization weight'))

    positivity_constraint = traits.Bool(True, usedefault=True, desc = ('Apply positivity constraint'))

    radial_order = traits.Int(8, usedefault=True,
                          desc=('radial order'))

    small_delta = traits.Float(0.02, mandatory=True,
                          desc=('Small data for gradient table (pulse duration)'))

    big_delta = traits.Float(0.5, mandatory=True,
                          desc=('Small data for gradient table (time interval)'))

    radial_order_values = traits.List([2,4,6,8,10,12])
    shore_radial_order = Enum(6,values='radial_order_values', usedefault=True, desc=('Even number that represents the order of the basis'))
    shore_zeta = traits.Int(700, usedefault=True, desc=('Scale factor'))
    shore_lambdaN = traits.Float(1e-8, usedefault=True,desc=('radial regularisation constant'))
    shore_lambdaL = traits.Float(1e-8, usedefault=True,desc=('angular regularisation constant'))
    shore_tau = traits.Float(0.025330295910584444,desc=('Diffusion time. By default the value that makes q equal to the square root of the b-value.'))

    shore_constrain_e0 = traits.Bool(False, usedefault=True,desc=('Constrain the optimization such that E(0) = 1.'))
    shore_positive_constraint = traits.Bool(False, usedefault=True,desc=('Constrain the propagator to be positive.'))

    traits_view = View(#Item('gradient_table',label='Gradient table (x,y,z,b):'),
                       Item('flip_table_axis',style='custom',label='Flip bvecs:'),
                       #Item('custom_gradient_table',enabled_when='gradient_table_file=="Custom..."'),
               #Item('b_value'),
               #Item('b0_volumes'),
                       Group(
                            Item('local_model',editor=EnumEditor(name='local_model_editor')),
                            Group(
                                Item('lmax_order'),
                                #Item('normalize_to_B0'),
                                Item('single_fib_thr',label = 'FA threshold'),
                                visible_when='local_model'
                                ),
                            visible_when='imaging_model != "DSI"'
                            ),
                       Group(
                            Item('shore_radial_order',label='Radial order'),
                            Item('shore_zeta',label='Scale factor (zeta)'),
                            Item('shore_lambdaN',label='Radial regularization constant'),
                            Item('shore_lambdaL',label='Angular regularization constant'),
                            Item('shore_tau',label='Diffusion time (s)'),
                            Item('shore_constrain_e0',label='Constrain the optimization such that E(0) = 1.'),
                            Item('shore_positive_constraint',label='Constrain the propagator to be positive.'),
                            label='Parameters of SHORE reconstruction model',
                            visible_when='imaging_model == "DSI"'
                            ),
                       Item('mapmri'),
                       Group(
                            VGroup(
                                Item('radial_order'),
                                HGroup(Item('small_delta'),Item('big_delta'))
                            ),
                            HGroup(
                                Item('laplacian_regularization'),Item('laplacian_weighting')
                            ),
                            Item('positivity_constraint'),
                            label="MAP_MRI settings",
                            visible_when='mapmri'
                       )
                    )



    def _recon_mode_changed(self,new):
        if new == 'Probabilistic' and self.imaging_model != 'DSI':
            self.local_model_editor = {True:'Constrained Spherical Deconvolution'}
            self.local_model = True
        elif new == 'Probabilistic' and self.imaging_model == 'DSI':
            pass
        else:
            self.local_model_editor = {False:'1:Tensor',True:'2:Constrained Spherical Deconvolution'}

class MRtrix_recon_config(HasTraits):
    # gradient_table = File
    flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))
    local_model_editor = Dict({False:'1:Tensor',True:'2:Constrained Spherical Deconvolution'})
    local_model = Bool(True)
    lmax_order = Enum(['Auto',2,4,6,8,10,12,14,16])
    normalize_to_B0 = Bool(False)
    single_fib_thr = Float(0.7,min=0,max=1)
    recon_mode = Str

    traits_view = View(#Item('gradient_table',label='Gradient table (x,y,z,b):'),
                       Item('flip_table_axis',style='custom',label='Flip gradient table:'),
                       #Item('custom_gradient_table',enabled_when='gradient_table_file=="Custom..."'),
		       #Item('b_value'),
		       #Item('b0_volumes'),
                       Item('local_model',editor=EnumEditor(name='local_model_editor')),
		       Group(Item('lmax_order'),
		       Item('normalize_to_B0'),
		       Item('single_fib_thr',label = 'FA threshold'),visible_when='local_model'),
                       )

    def _recon_mode_changed(self,new):
        if new == 'Probabilistic':
            self.local_model_editor = {True:'Constrained Spherical Deconvolution'}
            self.local_model = True
        else:
            self.local_model_editor = {False:'1:Tensor',True:'2:Constrained Spherical Deconvolution'}

class Camino_recon_config(HasTraits):
    b_value = Int (1000)
    model_type = Enum('Single-Tensor',['Single-Tensor','Two-Tensor','Three-Tensor','Other models'])
    singleTensor_models = {'dt':'Linear fit','nldt_pos':'Non linear positive semi-definite','nldt':'Unconstrained non linear','ldt_wtd':'Weighted linear'}
    local_model = Str('dt')
    local_model_editor = Dict(singleTensor_models)
    snr = Float(10.0)
    mixing_eq = Bool()
    fallback_model = Str('dt')
    fallback_editor = Dict(singleTensor_models)
    fallback_index = Int(1) # index for 'dt' which is the default fallback_model
    inversion = Int(1)

    gradient_table = File
    flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))

    traits_view = View(Item('gradient_table',label='Gradient table (x,y,z,b):'),
                       Item('flip_table_axis',style='custom',label='Flip table:'),
                       'model_type',
		               VGroup(Item('local_model',label="Camino model",editor=EnumEditor(name='local_model_editor')),
                              Item('snr',visible_when='local_model=="restore"'),
                              Item('mixing_eq',label='Compartment mixing parameter = 0.5',visible_when='model_type == "Two-Tensor" or model_type == "Three-Tensor"'),
                              Item('fallback_model',label='Initialisation and fallback model',editor=EnumEditor(name='fallback_editor'),visible_when='model_type == "Two-Tensor" or model_type == "Three-Tensor"')
                       )
                       )

    def _model_type_changed(self,new):
        if new == 'Single-Tensor':
            self.local_model_editor = self.singleTensor_models
            self.local_model = 'dt'
            self.mixing_eq = False
        elif new == 'Two-Tensor':
            self.local_model_editor = {'cylcyl':'Both Cylindrically symmetric','pospos':'Both positive','poscyl':'One positive, one cylindrically symmetric'}
            self.local_model = 'cylcyl'
        elif new == 'Three-Tensor':
            self.local_model_editor = {'cylcylcyl':'All cylindrically symmetric','pospospos':'All positive','posposcyl':'Two positive, one cylindrically symmetric','poscylcyl':'Two cylindrically symmetric, one positive'}
            self.local_model = 'cylcylcyl'
        elif new == 'Other models':
            self.local_model_editor = {'adc':'ADC','ball_stick':'Ball stick', 'restore':'Restore'}
            self.local_model = 'adc'
            self.mixing_eq = False

        self.update_inversion()

    def update_inversion(self):
        inversion_dict = {'ball_stick':-3, 'restore':-2, 'adc':-1, 'ltd':1, 'dt':1, 'nldt_pos':2,'nldt':4,'ldt_wtd':7,'cylcyl':10, 'pospos':30, 'poscyl':50, 'cylcylcyl':210, 'pospospos':230, 'posposcyl':250, 'poscylcyl':270}
        if self.model_type == 'Single-Tensor' or self.model_type == 'Other models':
            self.inversion = inversion_dict[self.local_model]
        else:
            self.inversion = inversion_dict[self.local_model] + inversion_dict[self.fallback_model]
            self.fallback_index = inversion_dict[self.fallback_model]
            if self.mixing_eq:
                self.inversion = self.inversion + 10

    def _local_model_changed(self,new):
        self.update_inversion()

    def _mixing_eq_changed(self,new):
        self.update_inversion()

    def _fallback_model_changed(self,new):
        self.update_inversion()

class FSL_recon_config(HasTraits):

    b_values = File()
    b_vectors = File()
    flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))

    # BEDPOSTX parameters
    burn_period = Int(0)
    fibres_per_voxel = Int(1)
    jumps = Int(1250)
    sampling = Int(25)
    weight = Float(1.00)

    traits_view = View('b_values',
                       'b_vectors',
                       Item('flip_table_axis',style='custom',label='Flip table:'),
                       VGroup('burn_period','fibres_per_voxel','jumps','sampling','weight',show_border=True,label = 'BEDPOSTX parameters'),
                      )

class Gibbs_recon_config(HasTraits):
    recon_model = Enum(['Tensor','CSD'])
    b_values = File()
    b_vectors = File()
    flip_table_axis = List(editor=CheckListEditor(values=['x','y','z'],cols=3))
    sh_order = Enum(4,[2,4,6,8,10,12,14,16])
    reg_lambda = Float(0.006)
    csa = Bool(True)

    traits_view = View(Item('recon_model',label='Reconstruction  model:'),
                       'b_values',
                       'b_vectors',
                       Item('flip_table_axis',style='custom',label='Flip table:'),
                       Group(Item('sh_order',label="Spherical Harmonics order"),
                             Item('reg_lambda', label="Regularisation lambda factor"),
                             Item('csa',label="Use constant solid angle"),
                             show_border=True,label='CSD parameters', visible_when='recon_model == "CSD"'),
	           )


# Nipype interfaces for DTB commands

class DTB_P0InputSpec(CommandLineInputSpec):
    dsi_basepath = traits.Str(desc='DSI path/basename (e.g. \"data/dsi_\")',position=1,mandatory=True,argstr = "--dsi %s")
    dwi_file = nibase.File(desc='DWI file',position=2,mandatory=True,exists=True,argstr = "--dwi %s")

class DTB_P0OutputSpec(TraitedSpec):
    out_file = nibase.File(desc='Resulting P0 file')

class DTB_P0(CommandLine):
    _cmd = 'DTB_P0'
    input_spec = DTB_P0InputSpec
    output_spec = DTB_P0OutputSpec

    def _list_outputs(self):
        outputs = self._outputs().get()
        path, base, _ = split_filename(self.inputs.dsi_basepath)
        outputs["out_file"]  = os.path.join(path,base+'P0.nii')
        return outputs

class DTB_gfaInputSpec(CommandLineInputSpec):
    dsi_basepath = traits.Str(desc='DSI path/basename (e.g. \"data/dsi_\")',position=1,mandatory=True,argstr = "--dsi %s")
    moment = traits.Enum((2, 3, 4),desc='Moment to calculate (2 = gfa, 3 = skewness, 4 = curtosis)',position=2,mandatory=True,argstr = "--m %s")

class DTB_gfaOutputSpec(TraitedSpec):
    out_file = nibase.File(desc='Resulting file')

class DTB_gfa(CommandLine):
    _cmd = 'DTB_gfa'
    input_spec = DTB_gfaInputSpec
    output_spec = DTB_gfaOutputSpec

    def _list_outputs(self):
        import shutil
        outputs = self._outputs().get()
        path, base, _ = split_filename(self.inputs.dsi_basepath)
        if self.inputs.moment == 2:
            shutil.move(os.path.join(path,base+'gfa.nii'),os.path.abspath(base+'gfa.nii'))
            outputs["out_file"]  = os.path.abspath(base+'gfa.nii')
        elif self.inputs.moment == 3:
            shutil.move(os.path.join(path,base+'skewness.nii'),os.path.abspath(base+'skewness.nii'))
            outputs["out_file"]  = os.path.abspath(base+'skewness.nii')
        elif self.inputs.moment == 4:
            shutil.move(os.path.join(path,base+'kurtosis.nii'),os.path.abspath(base+'kurtosis.nii'))
            outputs["out_file"]  = os.path.abspath(base+'kurtosis.nii')
        #if self.inputs.moment == 2:
        #    outputs["out_file"]  = os.path.join(path,base+'gfa.nii')
        #if self.inputs.moment == 3:
        #    outputs["out_file"]  = os.path.join(path,base+'skewness.nii')
        #if self.inputs.moment == 4:
        #    outputs["out_file"]  = os.path.join(path,base+'kurtosis.nii')

        return outputs

def strip_suffix(file_input, prefix):
    import os
    from nipype.utils.filemanip import split_filename
    path, _, _ = split_filename(file_input)
    return os.path.join(path, prefix+'_')

class flipTableInputSpec(BaseInterfaceInputSpec):
    table = File(exists=True)
    flipping_axis = List()
    delimiter = Str()
    header_lines = Int(0)
    orientation = Enum(['v','h'])

class flipTableOutputSpec(TraitedSpec):
    table = File(exists=True)

class flipTable(BaseInterface):
    input_spec = flipTableInputSpec
    output_spec = flipTableOutputSpec

    def _run_interface(self,runtime):
        axis_dict = {'x':0, 'y':1, 'z':2}
        import numpy as np
        f = open(self.inputs.table,'r')
        header = ''
        for h in range(self.inputs.header_lines):
            header += f.readline()
        if self.inputs.delimiter == ' ':
            table = np.loadtxt(f)
        else:
            table = np.loadtxt(f, delimiter=self.inputs.delimiter)
        f.close()
        if self.inputs.orientation == 'v':
            for i in self.inputs.flipping_axis:
                table[:,axis_dict[i]] = -table[:,axis_dict[i]]
        elif self.inputs.orientation == 'h':
            for i in self.inputs.flipping_axis:
                table[axis_dict[i],:] = -table[axis_dict[i],:]
        out_f = file(os.path.abspath('flipped_table.txt'),'a')
        if self.inputs.header_lines > 0:
            out_f.write(header)
        np.savetxt(out_f,table,delimiter=self.inputs.delimiter)
        out_f.close()
        return runtime

    def _list_outputs(self):
        outputs = self._outputs().get()
        outputs["table"] = os.path.abspath('flipped_table.txt')
        return outputs

def create_dtk_recon_flow(config):
    flow = pe.Workflow(name="reconstruction")

    # inputnode
    inputnode = pe.Node(interface=util.IdentityInterface(fields=["diffusion","diffusion_resampled"]),name="inputnode")

    outputnode = pe.Node(interface=util.IdentityInterface(fields=["DWI","B0","ODF","gFA","skewness","kurtosis","P0","max","V1"]),name="outputnode")

    if config.imaging_model == "DSI":
        prefix = "dsi"
        dtk_odfrecon = pe.Node(interface=dtk.ODFRecon(out_prefix=prefix),name='dtk_odfrecon')
        dtk_odfrecon.inputs.matrix = os.path.join(os.environ['DSI_PATH'],config.recon_matrix_file)
        config.dsi_number_of_directions
        config.number_of_output_directions
        dtk_odfrecon.inputs.n_b0 = config.number_of_b0_volumes
        dtk_odfrecon.inputs.n_directions = int(config.dsi_number_of_directions)+1
        dtk_odfrecon.inputs.n_output_directions = config.number_of_output_directions
        dtk_odfrecon.inputs.dsi = True

        flow.connect([
                    (inputnode,dtk_odfrecon,[('diffusion_resampled','DWI')]),
                    (dtk_odfrecon,outputnode,[('DWI','DWI'),('B0','B0'),('ODF','ODF'),('max','max')])])

    if config.imaging_model == "HARDI":
        prefix = "hardi"
        dtk_hardimat = pe.Node(interface=cmp_dtk.HARDIMat(),name='dtk_hardimat')

        #dtk_hardimat.inputs.gradient_table = config.gradient_table
        # Flip gradient table
        flip_table = pe.Node(interface=flipTable(),name='flip_table')
        flip_table.inputs.table = config.gradient_table
        flip_table.inputs.flipping_axis = config.flip_table_axis
        flip_table.inputs.delimiter = ','
        flip_table.inputs.header_lines = 0
        flip_table.inputs.orientation = 'v'
        flow.connect([
                (flip_table,dtk_hardimat,[("table","gradient_table")]),
                ])

        dtk_hardimat.inputs.oblique_correction = config.apply_gradient_orientation_correction

        dtk_odfrecon = pe.Node(interface=dtk.ODFRecon(out_prefix=prefix),name='dtk_odfrecon')
        dtk_odfrecon.inputs.n_b0 = config.number_of_b0_volumes
        dtk_odfrecon.inputs.n_directions = int(config.number_of_directions)+1
        dtk_odfrecon.inputs.n_output_directions = config.number_of_output_directions

        flow.connect([
                    (inputnode,dtk_hardimat,[('diffusion_resampled','reference_file')]),
                    (dtk_hardimat,dtk_odfrecon,[('out_file','matrix')]),
                    (inputnode,dtk_odfrecon,[('diffusion_resampled','DWI')]),
                    (dtk_odfrecon,outputnode,[('DWI','DWI'),('B0','B0'),('ODF','ODF'),('max','max')])])


    if config.imaging_model == "DTI":
        prefix = "dti"

        flip_table = pe.Node(interface=flipTable(),name='flip_table')
        flip_table.inputs.table = config.gradient_table
        flip_table.inputs.flipping_axis = config.flip_table_axis
        flip_table.inputs.delimiter = ','
        flip_table.inputs.header_lines = 0
        flip_table.inputs.orientation = 'v'

        dtk_dtirecon = pe.Node(interface=cmp_dtk.DTIRecon(out_prefix=prefix),name='dtk_dtirecon')
        dtk_dtirecon.inputs.b_value = config.maximum_b_value
        dtk_dtirecon.inputs.multiple_b_values = config.multiple_high_b_values
        dtk_dtirecon.inputs.n_averages = config.number_of_averages
        dtk_dtirecon.inputs.number_of_b0 = config.number_of_b0_volumes
        dtk_dtirecon.inputs.oblique_correction = config.apply_gradient_orientation_correction

        flow.connect([
                    (inputnode,dtk_dtirecon,[('diffusion','DWI')]),
                    (flip_table, dtk_dtirecon,[('table', 'gradient_matrix')]),
                    (dtk_dtirecon,outputnode,[('DWI','DWI'),('B0','B0'),('V1','V1')])])
    else:
        if 'gFA' in config.compute_additional_maps:
            dtb_gfa = pe.Node(interface=DTB_gfa(moment=2),name='dtb_gfa')
            flow.connect([
                        (dtk_odfrecon,dtb_gfa,[(('ODF',strip_suffix,prefix),'dsi_basepath')]),
                        (dtb_gfa,outputnode,[('out_file','gFA')])])
        if 'skewness' in config.compute_additional_maps:
            dtb_skewness = pe.Node(interface=DTB_gfa(moment=3),name='dtb_skewness')
            flow.connect([
                        (dtk_odfrecon,dtb_skewness,[(('ODF',strip_suffix,prefix),'dsi_basepath')]),
                        (dtb_skewness,outputnode,[('out_file','skewness')])])
        if 'kurtosis' in config.compute_additional_maps:
            dtb_kurtosis = pe.Node(interface=DTB_gfa(moment=4),name='dtb_kurtosis')
            flow.connect([
                        (dtk_odfrecon,dtb_kurtosis,[(('ODF',strip_suffix,prefix),'dsi_basepath')]),
                        (dtb_kurtosis,outputnode,[('out_file','kurtosis')])])
        if 'P0' in config.compute_additional_maps:
            dtb_p0 = pe.Node(interface=DTB_P0(),name='dtb_P0')
            flow.connect([
                        (inputnode,dtb_p0,[('diffusion','dwi_file')]),
                        (dtk_odfrecon,dtb_p0,[(('ODF',strip_suffix,prefix),'dsi_basepath')]),
                        (dtb_p0,outputnode,[('out_file','P0')])])

    return flow


def create_dipy_recon_flow(config):
    flow = pe.Workflow(name="reconstruction")
    inputnode = pe.Node(interface=util.IdentityInterface(fields=["diffusion","diffusion_resampled","brain_mask_resampled","wm_mask_resampled","bvals","bvecs"]),name="inputnode")
    outputnode = pe.Node(interface=util.IdentityInterface(fields=["DWI","FA","MSD","fod","model","eigVec","RF","grad","bvecs","mapmri_maps"],mandatory_inputs=True),name="outputnode")

    #Flip gradient table
    flip_bvecs = pe.Node(interface=flipBvec(),name='flip_bvecs')

    flip_bvecs.inputs.flipping_axis = config.flip_table_axis
    flip_bvecs.inputs.delimiter = ' '
    flip_bvecs.inputs.header_lines = 0
    flip_bvecs.inputs.orientation = 'h'
    flow.connect([
                (inputnode,flip_bvecs,[("bvecs","bvecs")]),
                (flip_bvecs,outputnode,[("bvecs_flipped","bvecs")])
                ])

    # Compute single fiber voxel mask
    dipy_erode = pe.Node(interface=Erode(out_filename="wm_mask_resampled.nii.gz"),name="dipy_erode")
    dipy_erode.inputs.number_of_passes = 1
    dipy_erode.inputs.filtertype = 'erode'

    flow.connect([
        (inputnode,dipy_erode,[("wm_mask_resampled",'in_file')])
        ])

    if config.imaging_model != 'DSI':
        # Tensor -> EigenVectors / FA, AD, MD, RD maps
        dipy_tensor = pe.Node(interface=DTIEstimateResponseSH(),name='dipy_tensor')
        dipy_tensor.inputs.auto = True
        dipy_tensor.inputs.roi_radius = 10
        dipy_tensor.inputs.fa_thresh = config.single_fib_thr

        flow.connect([
            (inputnode, dipy_tensor,[('diffusion_resampled','in_file')]),
            (inputnode, dipy_tensor,[('bvals','in_bval')]),
            (flip_bvecs, dipy_tensor,[('bvecs_flipped','in_bvec')]),
            (dipy_erode, dipy_tensor,[('out_file','in_mask')])
            ])

        flow.connect([
            (dipy_tensor,outputnode,[("response","RF")]),
            (dipy_tensor,outputnode,[("fa_file","FA")])
            ])

        if not config.local_model:
            flow.connect([
                (inputnode,outputnode,[('diffusion_resampled','DWI')]),
                (dipy_tensor,outputnode,[("dti_model","model")])
                ])
            # Tensor -> Eigenvectors
            # mrtrix_eigVectors = pe.Node(interface=Tensor2Vector(),name="mrtrix_eigenvectors")

        # Constrained Spherical Deconvolution
        else:
            # Perform spherical deconvolution
            dipy_CSD = pe.Node(interface=CSD(),name="dipy_CSD")

            # if config.tracking_processing_tool != 'Dipy':
            dipy_CSD.inputs.save_shm_coeff = True
            dipy_CSD.inputs.out_shm_coeff='diffusion_shm_coeff.nii.gz'

            # dipy_CSD.inputs.save_fods=True
            # dipy_CSD.inputs.out_fods='diffusion_fODFs.nii.gz'

            if config.lmax_order != 'Auto':
                dipy_CSD.inputs.sh_order = config.lmax_order

            dipy_CSD.inputs.fa_thresh = config.single_fib_thr

            flow.connect([
                    (inputnode, dipy_CSD,[('diffusion_resampled','in_file')]),
                    (inputnode, dipy_CSD,[('bvals','in_bval')]),
                    (flip_bvecs, dipy_CSD,[('bvecs_flipped','in_bvec')]),
                    # (dipy_tensor, dipy_CSD,[('out_mask','in_mask')]),
                    # (dipy_erode, dipy_CSD,[('out_file','in_mask')]),
                    (inputnode,dipy_CSD,[("brain_mask_resampled",'in_mask')]),
                    #(dipy_tensor, dipy_CSD,[('response','response')]),
                    (dipy_CSD,outputnode,[('model','model')])
                    ])

            if config.tracking_processing_tool != 'Dipy':
                flow.connect([
                        (dipy_CSD,outputnode,[('out_shm_coeff','DWI')])
                        ])
            else:
                flow.connect([
                        (inputnode,outputnode,[('diffusion_resampled','DWI')])
                        ])
    else:
        # Perform SHORE reconstruction (DSI)

        dipy_SHORE = pe.Node(interface=SHORE(),name="dipy_SHORE")

        if config.tracking_processing_tool == 'MRtrix':
            dipy_SHORE.inputs.tracking_processing_tool = 'mrtrix'
        elif config.tracking_processing_tool == 'Dipy':
            dipy_SHORE.inputs.tracking_processing_tool = 'dipy'

        # if config.tracking_processing_tool != 'Dipy':
        #dipy_SHORE.inputs.save_shm_coeff = True
        #dipy_SHORE.inputs.out_shm_coeff='diffusion_shm_coeff.nii.gz'

        dipy_SHORE.inputs.radial_order = int(config.shore_radial_order)
        dipy_SHORE.inputs.zeta = config.shore_zeta
        dipy_SHORE.inputs.lambdaN = config.shore_lambdaN
        dipy_SHORE.inputs.lambdaL = config.shore_lambdaL
        dipy_SHORE.inputs.tau = config.shore_tau
        dipy_SHORE.inputs.constrain_e0 = config.shore_constrain_e0
        dipy_SHORE.inputs.positive_constraint = config.shore_positive_constraint
        #dipy_SHORE.inputs.save_shm_coeff = True
        #dipy_SHORE.inputs.out_shm_coeff = 'diffusion_shore_shm_coeff.nii.gz'

        flow.connect([
                (inputnode, dipy_SHORE,[('diffusion_resampled','in_file')]),
                (inputnode, dipy_SHORE,[('bvals','in_bval')]),
                (flip_bvecs, dipy_SHORE,[('bvecs_flipped','in_bvec')]),
                # (dipy_tensor, dipy_CSD,[('out_mask','in_mask')]),
                # (dipy_erode, dipy_SHORE,[('out_file','in_mask')]),
                #(inputnode,dipy_SHORE,[("wm_mask_resampled",'in_mask')]),
                (inputnode,dipy_SHORE,[("brain_mask_resampled",'in_mask')]),
                #(dipy_tensor, dipy_CSD,[('response','response')]),
                (dipy_SHORE,outputnode,[('model','model')]),
                (dipy_SHORE,outputnode,[('fod','fod')]),
                (dipy_SHORE,outputnode,[('GFA','FA')])
                ])


        flow.connect([
                (inputnode,outputnode,[('diffusion_resampled','DWI')])
                ])


    if config.mapmri:
        from cmp.interfaces.dipy import MAPMRI
        dipy_MAPMRI = pe.Node(interface=MAPMRI(),name='dipy_mapmri')

        dipy_MAPMRI.inputs.laplacian_regularization = config.laplacian_regularization
        dipy_MAPMRI.inputs.laplacian_weighting= config.laplacian_weighting
        dipy_MAPMRI.inputs.positivity_constraint = config.positivity_constraint
        dipy_MAPMRI.inputs.radial_order = config.radial_order
        dipy_MAPMRI.inputs.small_delta = config.small_delta
        dipy_MAPMRI.inputs.big_delta = config.big_delta

        maps_merge = pe.Node(interface=util.Merge(8),name="merge_additional_maps")

        flow.connect([
                (inputnode, dipy_MAPMRI,[('diffusion_resampled','in_file')]),
                (inputnode, dipy_MAPMRI,[('bvals','in_bval')]),
                (flip_bvecs, dipy_MAPMRI,[('bvecs_flipped','in_bvec')]),
                (dipy_MAPMRI, maps_merge, [('rtop_file','in1'),('rtap_file','in2'),('rtpp_file','in3'),('msd_file','in4'),('qiv_file','in5'),('ng_file','in6'),('ng_perp_file','in7'),('ng_para_file','in8')])
                ])

    return flow


def create_mrtrix_recon_flow(config):
    flow = pe.Workflow(name="reconstruction")
    inputnode = pe.Node(interface=util.IdentityInterface(fields=["diffusion","diffusion_resampled","wm_mask_resampled","grad"]),name="inputnode")
    outputnode = pe.Node(interface=util.IdentityInterface(fields=["DWI","FA","ADC","eigVec","RF","grad"],mandatory_inputs=True),name="outputnode")

    # Flip gradient table
    flip_table = pe.Node(interface=flipTable(),name='flip_table')

    flip_table.inputs.flipping_axis = config.flip_table_axis
    flip_table.inputs.delimiter = ' '
    flip_table.inputs.header_lines = 0
    flip_table.inputs.orientation = 'v'
    flow.connect([
                (inputnode,flip_table,[("grad","table")]),
                (flip_table,outputnode,[("table","grad")])
                ])
    # flow.connect([
    #             (inputnode,outputnode,[("grad","grad")])
    #             ])

    # Tensor
    mrtrix_tensor = pe.Node(interface=DWI2Tensor(),name='mrtrix_make_tensor')

    flow.connect([
		(inputnode, mrtrix_tensor,[('diffusion_resampled','in_file')]),
        (flip_table,mrtrix_tensor,[("table","encoding_file")]),
		])

    # Tensor -> FA map
    mrtrix_tensor_metrics = pe.Node(interface=TensorMetrics(out_fa='FA.mif',out_adc='ADC.mif'),name='mrtrix_tensor_metrics')
    convert_FA = pe.Node(interface=MRConvert(out_filename="FA.nii.gz"),name='convert_FA')
    convert_ADC = pe.Node(interface=MRConvert(out_filename="ADC.nii.gz"),name='convert_ADC')

    flow.connect([
		(mrtrix_tensor,mrtrix_tensor_metrics,[('tensor','in_file')]),
		(mrtrix_tensor_metrics,convert_FA,[('out_fa','in_file')]),
        (mrtrix_tensor_metrics,convert_ADC,[('out_adc','in_file')]),
        (convert_FA,outputnode,[("converted","FA")]),
        (convert_ADC,outputnode,[("converted","ADC")])
		])

    # Tensor -> Eigenvectors
    mrtrix_eigVectors = pe.Node(interface=Tensor2Vector(),name="mrtrix_eigenvectors")

    flow.connect([
		(mrtrix_tensor,mrtrix_eigVectors,[('tensor','in_file')]),
		(mrtrix_eigVectors,outputnode,[('vector','eigVec')])
		])

    # Constrained Spherical Deconvolution
    if config.local_model:
        print "CSD true"
        # Compute single fiber voxel mask
        mrtrix_erode = pe.Node(interface=Erode(out_filename='wm_mask_res_eroded.nii.gz'),name="mrtrix_erode")
        mrtrix_erode.inputs.number_of_passes = 1
        mrtrix_erode.inputs.filtertype = 'erode'
        mrtrix_mul_eroded_FA = pe.Node(interface=MRtrix_mul(),name='mrtrix_mul_eroded_FA')
        mrtrix_mul_eroded_FA.inputs.out_filename = "diffusion_resampled_tensor_FA_masked.mif"
        mrtrix_thr_FA = pe.Node(interface=MRThreshold(out_file='FA_th.mif'),name='mrtrix_thr')
        mrtrix_thr_FA.inputs.abs_value = config.single_fib_thr

        flow.connect([
		    (inputnode,mrtrix_erode,[("wm_mask_resampled",'in_file')]),
		    (mrtrix_erode,mrtrix_mul_eroded_FA,[('out_file','input2')]),
		    (mrtrix_tensor_metrics,mrtrix_mul_eroded_FA,[('out_fa','input1')]),
		    (mrtrix_mul_eroded_FA,mrtrix_thr_FA,[('out_file','in_file')])
		    ])
        # Compute single fiber response function
        mrtrix_rf = pe.Node(interface=EstimateResponseForSH(),name="mrtrix_rf")
        #if config.lmax_order != 'Auto':
        mrtrix_rf.inputs.maximum_harmonic_order = 6 #int(config.lmax_order)

        mrtrix_rf.inputs.algorithm='tournier'
        #mrtrix_rf.inputs.normalise = config.normalize_to_B0
        flow.connect([
		    (inputnode,mrtrix_rf,[("diffusion_resampled","in_file")]),
		    (mrtrix_thr_FA,mrtrix_rf,[("thresholded","mask_image")]),
            (flip_table,mrtrix_rf,[("table","encoding_file")]),
		    ])

        # Perform spherical deconvolution
        mrtrix_CSD = pe.Node(interface=ConstrainedSphericalDeconvolution(),name="mrtrix_CSD")
        mrtrix_CSD.inputs.algorithm = 'csd'
        #mrtrix_CSD.inputs.normalise = config.normalize_to_B0
        flow.connect([
		    (inputnode,mrtrix_CSD,[('diffusion_resampled','in_file')]),
		    (mrtrix_rf,mrtrix_CSD,[('response','response_file')]),
		    (mrtrix_rf,outputnode,[('response','RF')]),
		    (inputnode,mrtrix_CSD,[("wm_mask_resampled",'mask_image')]),
            (flip_table,mrtrix_CSD,[("table","encoding_file")]),
		    (mrtrix_CSD,outputnode,[('spherical_harmonics_image','DWI')])
		    ])
    else:
        flow.connect([
		    (inputnode,outputnode,[('diffusion_resampled','DWI')])
		    ])

    return flow

def create_camino_recon_flow(config):
    flow = pe.Workflow(name="reconstruction")
    inputnode = pe.Node(interface=util.IdentityInterface(fields=["diffusion","diffusion_resampled","wm_mask_resampled"]),name="inputnode")
    outputnode = pe.Node(interface=util.IdentityInterface(fields=["DWI","FA","MD","eigVec","RF","SD","grad"],mandatory_inputs=True),name="outputnode")

    # Flip gradient table
    flip_table = pe.Node(interface=flipTable(),name='flip_table')
    flip_table.inputs.table = config.gradient_table
    flip_table.inputs.flipping_axis = config.flip_table_axis
    flip_table.inputs.delimiter = ' '
    flip_table.inputs.header_lines = 2
    flip_table.inputs.orientation = 'v'
    flow.connect([
                (flip_table,outputnode,[("table","grad")]),
                ])

    # Convert diffusion data to camino format
    camino_convert = pe.Node(interface=camino.Image2Voxel(),name='camino_convert')
    flow.connect([
		(inputnode,camino_convert,[('diffusion_resampled','in_file')])
		])

    # Fit model
    camino_ModelFit = pe.Node(interface=camino.ModelFit(),name='camino_ModelFit')
    if config.model_type == "Two-Tensor" or config.model_type == "Three-Tensor":
        if config.mixing_eq:
            camino_ModelFit.inputs.model = config.local_model + '_eq ' + config.fallback_model
        else:
            camino_ModelFit.inputs.model = config.local_model + ' ' + config.fallback_model
    else:
        camino_ModelFit.inputs.model = config.local_model

    if config.local_model == 'restore':
        camino_ModelFit.inputs.sigma = config.snr

    flow.connect([
		(camino_convert,camino_ModelFit,[('voxel_order','in_file')]),
		(inputnode,camino_ModelFit,[('wm_mask_resampled','bgmask')]),
        (flip_table,camino_ModelFit,[("table","scheme_file")]),
		(camino_ModelFit,outputnode,[('fitted_data','DWI')])
		])

    # Compute FA map
    camino_FA = pe.Node(interface=camino.ComputeFractionalAnisotropy(),name='camino_FA')
    if config.model_type == 'Single-Tensor' or config.model_type == 'Other models':
        camino_FA.inputs.inputmodel = 'dt'
    elif config.model_type == 'Two-Tensor':
        camino_FA.inputs.inputmodel = 'twotensor'
    elif config.model_type == 'Three-Tensor':
        camino_FA.inputs.inputmodel = 'threetensor'
    elif config.model_type == 'Multitensor':
        camino_FA.inputs.inputmodel = 'multitensor'

    convert_FA = pe.Node(interface=camino.Voxel2Image(output_root="FA"),name="convert_FA")

    flow.connect([
		(camino_ModelFit,camino_FA,[('fitted_data','in_file')]),
		(camino_FA,convert_FA,[("fa","in_file")]),
        (inputnode,convert_FA,[("wm_mask_resampled","header_file")]),
        (convert_FA,outputnode,[('image_file','FA')]),
		])

    # Compute MD map
    camino_MD = pe.Node(interface=camino.ComputeMeanDiffusivity(),name='camino_MD')
    if config.model_type == 'Single-Tensor' or config.model_type == 'Other models':
        camino_MD.inputs.inputmodel = 'dt'
    elif config.model_type == 'Two-Tensor':
        camino_MD.inputs.inputmodel = 'twotensor'
    elif config.model_type == 'Three-Tensor':
        camino_MD.inputs.inputmodel = 'threetensor'
    elif config.model_type == 'Multitensor':
        camino_MD.inputs.inputmodel = 'multitensor'

    flow.connect([
		(camino_ModelFit,camino_MD,[('fitted_data','in_file')]),
		(camino_MD,outputnode,[('md','MD')]),
		])

    # Compute Eigenvalues
    camino_eigenvectors = pe.Node(interface=camino.ComputeEigensystem(),name='camino_eigenvectors')
    if config.model_type == 'Single-Tensor' or config.model_type == 'Other models':
        camino_eigenvectors.inputs.inputmodel = 'dt'
    else:
        camino_eigenvectors.inputs.inputmodel = 'multitensor'
        if config.model_type == 'Three-Tensor':
            camino_eigenvectors.inputs.maxcomponents = 3
        elif config.model_type == 'Two-Tensor':
            camino_eigenvectors.inputs.maxcomponents = 2

    flow.connect([
		(camino_ModelFit,camino_eigenvectors,[('fitted_data','in_file')]),
		(camino_eigenvectors,outputnode,[('eigen','eigVec')])
		])
    return flow

def create_fsl_recon_flow(config):
    flow = pe.Workflow(name="reconstruction")

    inputnode = pe.Node(interface=util.IdentityInterface(fields=["diffusion_resampled","wm_mask_resampled"]),name="inputnode")
    outputnode = pe.Node(interface=util.IdentityInterface(fields=["phsamples","fsamples","thsamples"],mandatory_inputs=True),name="outputnode")

    # Flip gradient table
    flip_table = pe.Node(interface=flipTable(),name='flip_table')
    flip_table.inputs.table = config.b_vectors
    flip_table.inputs.flipping_axis = config.flip_table_axis
    flip_table.inputs.delimiter = ' '
    flip_table.inputs.header_lines = 0
    flip_table.inputs.orientation = 'h'

    fsl_node = pe.Node(interface=fsl.BEDPOSTX(),name='BEDPOSTX')

    fsl_node.inputs.bvals = config.b_values
    fsl_node.inputs.burn_period = config.burn_period
    fsl_node.inputs.fibres = config.fibres_per_voxel
    fsl_node.inputs.jumps = config.jumps
    fsl_node.inputs.sampling = config.sampling
    fsl_node.inputs.weight = config.weight

    flow.connect([
                (inputnode,fsl_node,[("diffusion_resampled","dwi")]),
                (inputnode,fsl_node,[("wm_mask_resampled","mask")]),
                (flip_table,fsl_node,[("table","bvecs")]),
                (fsl_node,outputnode,[("merged_fsamples","fsamples")]),
                (fsl_node,outputnode,[("merged_phsamples","phsamples")]),
                (fsl_node,outputnode,[("merged_thsamples","thsamples")]),
                ])

    return flow

class MITKqball_commandInputSpec(CommandLineInputSpec):
    in_file = File(argstr="-i %s",position = 1,mandatory=True,exists=True,desc="input raw dwi (.dwi or .fsl/.fslgz)")
    out_file_name = String(argstr="-o %s",position=2,desc='output fiber name (.dti)')
    sh_order = Int(argstr="-sh %d", position=3,des='spherical harmonics order (optional), (default: 4)')
    reg_lambda = Float(argstr="-r %0.4f", position=4, desc='ragularization factor lambda (optional), (default: 0.006)')
    csa = Bool(argstr="-csa", position=5, desc='use constant solid angle consideration (optional)')

class MITKqball_commandOutputSpec(TraitedSpec):
    out_file = File(exists=True,desc='output tensor file')

class MITKqball(CommandLine):
    _cmd = 'MitkQballReconstruction.sh'
    input_spec = MITKqball_commandInputSpec
    output_spec = MITKqball_commandOutputSpec

    def _list_outputs(self):
        outputs = self._outputs().get()
        outputs["out_file"] = self.inputs.out_file_name
        return outputs

class MITKtensor_commandInputSpec(CommandLineInputSpec):
    in_file = File(argstr="-i %s",position = 1,mandatory=True,exists=True,desc="input raw dwi (.dwi or .fsl/.fslgz)")
    out_file_name = String(argstr="-o %s",position=2,desc='output fiber name (.dti)')

class MITKtensor_commandOutputSpec(TraitedSpec):
    out_file = File(exists=True,desc='output tensor file')

class MITKtensor(CommandLine):
    _cmd = 'MitkTensorReconstruction.sh'
    input_spec = MITKtensor_commandInputSpec
    output_spec = MITKtensor_commandOutputSpec

    def _list_outputs(self):
        outputs = self._outputs().get()
        outputs["out_file"] = self.inputs.out_file_name
        return outputs

class gibbs_reconInputSpec(BaseInterfaceInputSpec):

    dwi = File(exists=True)
    bvals = File(exists=True)
    bvecs = File(exists=True)
    recon_model = Enum(['Tensor','CSD'])
    sh_order = Int(argstr="-sh %d", position=3,des='spherical harmonics order (optional), (default: 4)')
    reg_lambda = Float(argstr="-t %0.4f", position=4, desc='ragularization factor lambda (optional), (default: 0.006)')
    csa = Bool(argstr="-csa", position=5, desc='use constant solid angle consideration (optional)')

class gibbs_reconOutputSpec(TraitedSpec):
    recon_file = File(exists=True)

class gibbs_recon(BaseInterface):
    input_spec = gibbs_reconInputSpec
    output_spec = gibbs_reconOutputSpec

    def _run_interface(self,runtime):
        # change DWI and gradient table names
        mitk_dwi = os.path.abspath(os.path.basename(self.inputs.dwi)+'.fsl')
        shutil.copyfile(self.inputs.dwi,mitk_dwi)
        mitk_bvec = os.path.abspath(os.path.basename(self.inputs.dwi)+'.fsl.bvecs')
        shutil.copyfile(self.inputs.bvecs,mitk_bvec)
        mitk_bval = os.path.abspath(os.path.basename(self.inputs.dwi)+'.fsl.bvals')
        shutil.copyfile(self.inputs.bvals,mitk_bval)
        if self.inputs.recon_model == 'Tensor':
            tensor = pe.Node(interface=MITKtensor(in_file = mitk_dwi, out_file_name = os.path.abspath('mitk_tensor.dti')),name="mitk_tensor")
            res = tensor.run()
        elif self.inputs.recon_model == 'CSD':
            csd = pe.Node(interface=MITKqball(),name='mitk_CSD')
            csd.inputs.in_file = mitk_dwi
            csd.inputs.out_file_name = os.path.abspath('mitk_qball.qbi')
            csd.inputs.sh_order = self.inputs.sh_order
            csd.inputs.reg_lambda = self.inputs.reg_lambda
            csd.inputs.csa = self.inputs.csa
            res = csd.run()

        return runtime

    def _list_outputs(self):
        outputs = self._outputs().get()
        if self.inputs.recon_model == 'Tensor':
            outputs["recon_file"] = os.path.abspath('mitk_tensor.dti')
        elif self.inputs.recon_model == 'CSD':
            outputs["recon_file"] = os.path.abspath('mitk_qball.qbi')
        return outputs

def create_gibbs_recon_flow(config):
    flow = pe.Workflow(name="reconstruction")

    inputnode = pe.Node(interface=util.IdentityInterface(fields=["diffusion_resampled"]),name="inputnode")
    outputnode = pe.Node(interface=util.IdentityInterface(fields=["recon_file"],mandatory_inputs=True),name="outputnode")

    # Flip gradient table
    flip_table = pe.Node(interface=flipTable(),name='flip_table')
    flip_table.inputs.table = config.b_vectors
    flip_table.inputs.flipping_axis = config.flip_table_axis
    flip_table.inputs.delimiter = ' '
    flip_table.inputs.header_lines = 0
    flip_table.inputs.orientation = 'h'

    gibbs_node = pe.Node(interface=gibbs_recon(),name='gibbs_reconstruction')
    gibbs_node.inputs.bvals = config.b_values
    gibbs_node.inputs.recon_model = config.recon_model
    gibbs_node.inputs.sh_order = config.sh_order
    gibbs_node.inputs.reg_lambda = config.reg_lambda
    gibbs_node.inputs.csa = config.csa

    flow.connect([
                  (flip_table,gibbs_node,[("table","bvecs")]),
                  (inputnode,gibbs_node,[("diffusion_resampled","dwi")]),
                  (gibbs_node,outputnode,[("recon_file","recon_file")])
                ])
    return flow