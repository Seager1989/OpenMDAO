import os
import tempfile
import shutil
from six.moves import range
import unittest
import itertools
from six import iterkeys

try:
    from parameterized import parameterized
except ImportError:
    from openmdao.utils.assert_utils import SkipParameterized as parameterized

import numpy as np
from scipy.sparse import coo_matrix

from openmdao.api import Problem, Group, IndepVarComp, ImplicitComponent, ExecComp, \
    ExplicitComponent, NonlinearBlockGS, pyOptSparseDriver
from openmdao.utils.assert_utils import assert_rel_error
from openmdao.utils.mpi import MPI
from openmdao.utils.coloring import compute_total_coloring

from openmdao.test_suite.components.impl_comp_array import TestImplCompArray, TestImplCompArrayDense

from openmdao.test_suite.components.simple_comps import DoubleArrayComp, NonSquareArrayComp

try:
    from openmdao.parallel_api import PETScVector
    vector_class = PETScVector
except ImportError:
    vector_class = DefaultVector
    PETScVector = None

from openmdao.utils.general_utils import set_pyoptsparse_opt

# check that pyoptsparse is installed. if it is, try to use SLSQP.
OPT, OPTIMIZER = set_pyoptsparse_opt('SLSQP')


def setup_vars(self, ofs, wrts):
    matrix = self.sparsity
    isplit = self.isplit
    osplit = self.osplit

    shapesz = matrix.shape[1]
    sz = shapesz // isplit
    rem = shapesz % isplit
    for i in range(isplit):
        if rem > 0:
            isz = sz + rem
            rem = 0
        else:
            isz = sz

        self.add_input('x%d' % i, np.zeros(isz))

    shapesz = matrix.shape[0]
    sz = shapesz // osplit
    rem = shapesz % osplit
    for i in range(osplit):
        if rem > 0:
            isz = sz + rem
            rem = 0
        else:
            isz = sz
        self.add_output('y%d' % i, np.zeros(isz))

    self.declare_partials(of=ofs, wrt=wrts, method=self.method)


class CounterGroup(Group):
    def __init__(self, *args, **kwargs):
        super(CounterGroup, self).__init__(*args, **kwargs)
        self._nruns = 0

    def _solve_nonlinear(self, *args, **kwargs):
        super(CounterGroup, self)._solve_nonlinear(*args, **kwargs)
        self._nruns += 1


class SparseCompImplicit(ImplicitComponent):

    def __init__(self, sparsity, method='fd', isplit=1, osplit=1, **kwargs):
        super(SparseCompImplicit, self).__init__(**kwargs)
        self.sparsity = sparsity
        self.isplit = isplit
        self.osplit = osplit
        self.method = method
        self._nruns = 0


    def setup(self):
        setup_vars(self, ofs='*', wrts='*')

    # this is defined for easier testing of coloring of approx partials
    def apply_nonlinear(self, inputs, outputs, residuals):
        prod = self.sparsity.dot(inputs._data) - outputs._data
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            residuals[outname] = prod[start:end]
            start = end
        self._nruns += 1


    # this is defined so we can more easily test coloring of approx totals in a Group above this comp
    def solve_nonlinear(self, inputs, outputs):
        prod = self.sparsity.dot(inputs._data)
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            outputs[outname] = prod[start:end]
            start = end
        self._nruns += 1


class SparseCompExplicit(ExplicitComponent):

    def __init__(self, sparsity, method='fd', isplit=1, osplit=1, **kwargs):
        super(SparseCompExplicit, self).__init__(**kwargs)
        self.sparsity = sparsity
        self.isplit = isplit
        self.osplit = osplit
        self.method = method
        self._nruns = 0

    def setup(self):
        setup_vars(self, ofs='*', wrts='*')

    def compute(self, inputs, outputs):
        prod = self.sparsity.dot(inputs._data)
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            outputs[outname] = prod[start:end]
            start = end
        self._nruns += 1


def _check_partial_matrix(system, jac, expected):
    blocks = []
    for of in system._var_allprocs_abs_names['output']:
        cblocks = []
        for wrt in system._var_allprocs_abs_names['input']:
            key = (of, wrt)
            if key in jac:
                cblocks.append(jac[key]['value'])
        if cblocks:
            blocks.append(np.hstack(cblocks))
    fullJ = np.vstack(blocks)
    np.testing.assert_almost_equal(fullJ, expected)


def _check_total_matrix(system, jac, expected):
    blocks = []
    for of in system._var_allprocs_abs_names['output']:
        cblocks = []
        for wrt in itertools.chain(system._var_allprocs_abs_names['output'], system._var_allprocs_abs_names['input']):
            key = (of, wrt)
            if key in jac:
                cblocks.append(jac[key])
        if cblocks:
            blocks.append(np.hstack(cblocks))
    fullJ = np.vstack(blocks)
    np.testing.assert_almost_equal(fullJ, expected)


def _check_semitotal_matrix(system, jac, expected):
    blocks = []
    for of in system._var_allprocs_abs_names['output']:
        cblocks = []
        for wrt in itertools.chain(system._var_allprocs_abs_names['output'], system._var_allprocs_abs_names['input']):
            key = (of, wrt)
            if key in jac:
                rows = jac[key]['rows']
                if rows is not None:
                    cols = jac[key]['cols']
                    val = coo_matrix((jac[key]['value'], (rows, cols)), shape=jac[key]['shape']).toarray()
                else:
                    val = jac[key]['value']
                cblocks.append(val)
        if cblocks:
            blocks.append(np.hstack(cblocks))
    fullJ = np.vstack(blocks)
    np.testing.assert_almost_equal(fullJ, expected)


class TestCSColoring(unittest.TestCase):
    FD_METHOD = 'cs'

    def setUp(self):
        self.startdir = os.getcwd()
        self.tempdir = tempfile.mkdtemp(prefix=self.__class__.__name__ + '_')
        os.chdir(self.tempdir)

    def tearDown(self):
        os.chdir(self.startdir)
        try:
            shutil.rmtree(self.tempdir)
        except OSError:
            pass

    def test_simple_partials_explicit(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        sparsity = np.array(
                [[1, 0, 0, 1, 1, 1, 0],
                 [0, 1, 0, 1, 0, 1, 1],
                 [0, 1, 0, 1, 1, 1, 0],
                 [1, 0, 0, 0, 0, 1, 0],
                 [0, 1, 1, 0, 1, 1, 1]], dtype=float
            )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.random.random(4))
        indeps.add_output('x1', np.random.random(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        comp.declare_coloring('x*', method=self.FD_METHOD)
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        comp.run_linearize()
        prob.run_model()
        start_nruns = comp._nruns
        comp.run_linearize()
        self.assertEqual(comp._nruns - start_nruns, 5)
        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity)

    def test_simple_partials_explicit_shape_bug(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        # create sparsity with last row and col all zeros.
        # bug happened when we created a COO matrix without supplying shape
        sparsity = np.array(
                [[1, 0, 0, 1, 1, 1, 0],
                 [0, 1, 0, 1, 0, 1, 0],
                 [0, 1, 0, 1, 1, 1, 0],
                 [1, 0, 0, 0, 0, 1, 0],
                 [0, 0, 0, 0, 0, 0, 0]], dtype=float
            )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.random.random(4))
        indeps.add_output('x1', np.random.random(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        comp.declare_coloring('x*', method=self.FD_METHOD)

        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        comp._linearize()

    def test_simple_partials_implicit(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        sparsity = np.array(
            [[1, 0, 0, 1, 1, 1, 0],
             [0, 1, 0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1, 1, 0],
             [1, 0, 0, 0, 0, 1, 0],
             [0, 1, 1, 0, 1, 1, 1]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        comp.declare_coloring('x*', method=self.FD_METHOD)
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        comp.run_linearize()  # trigger dynamic coloring
        prob.run_model()   # get a good point to linearize around
        start_nruns = comp._nruns
        comp.run_linearize()
        # add 5 to number of runs to cover the 5 uncolored output columns
        self.assertEqual(comp._nruns - start_nruns, sparsity.shape[0] + 5)
        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity)

    def test_simple_semitotals(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        sub = model.add_subsystem('sub', CounterGroup())
        sub.declare_coloring('*', method=self.FD_METHOD)
        comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'sub.comp.x0')
        model.connect('indeps.x1', 'sub.comp.x1')

        model.sub.comp.add_constraint('y0')
        model.sub.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        derivs = prob.driver._compute_totals()  # this is when the dynamic coloring update happens

        start_nruns = sub._nruns
        derivs = prob.driver._compute_totals()
        self.assertEqual(sub._nruns - start_nruns, 3)
        _check_partial_matrix(sub, sub._jacobian._subjacs_info, sparsity)

    @unittest.skipUnless(OPTIMIZER, 'requires pyoptsparse SLSQP.')
    def test_simple_totals(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = CounterGroup()
        prob.driver = pyOptSparseDriver(optimizer='SLSQP')
        prob.driver.declare_coloring()
        prob.driver.options['dynamic_derivs_repeats'] = 1

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')
        model.declare_coloring('*', method=self.FD_METHOD, step=1e-6 if self.FD_METHOD=='fd' else None)

        model.comp.add_objective('y0', index=0)  # pyoptsparse SLSQP requires a scalar objective, so pick index 0
        model.comp.add_constraint('y1', lower=[1., 2.])
        model.add_design_var('indeps.x0', lower=np.ones(3), upper=np.ones(3)+.1)
        model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)
        model.approx_totals(method=self.FD_METHOD)
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_driver()  # need this to trigger the dynamic coloring

        prob.driver._total_jac = None

        start_nruns = model._nruns
        derivs = prob.compute_totals()
        _check_total_matrix(model, derivs, sparsity[[0,3,4],:])
        nruns = model._nruns - start_nruns
        self.assertEqual(nruns, 3)

    def test_totals_over_implicit_comp(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = CounterGroup()
        prob.driver = pyOptSparseDriver(optimizer='SLSQP')
        prob.driver.declare_coloring()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.nonlinear_solver = NonlinearBlockGS()
        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_objective('y0', index=1)
        model.comp.add_constraint('y1', lower=[1., 2.])
        model.add_design_var('indeps.x0', lower=np.ones(3), upper=np.ones(3)+.1)
        model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)


        model.approx_totals(method=self.FD_METHOD)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_driver()  # need this to trigger the dynamic coloring

        prob.driver._total_jac = None

        start_nruns = model._nruns
        derivs = prob.driver._compute_totals()
        self.assertEqual(model._nruns - start_nruns, 3)
        rows = [1,3,4]
        _check_total_matrix(model, derivs, sparsity[rows, :])

    def test_totals_of_wrt_indices(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = CounterGroup()
        prob.driver = pyOptSparseDriver(optimizer='SLSQP')
        prob.driver.declare_coloring()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        # model.declare_coloring('*', method=self.FD_METHOD)
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_objective('y0', index=1)
        model.comp.add_constraint('y1', lower=[1., 2.])
        model.add_design_var('indeps.x0',  indices=[0,2], lower=np.ones(2), upper=np.ones(2)+.1)
        model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)

        model.approx_totals(method=self.FD_METHOD)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_driver()  # need this to trigger the dynamic coloring

        prob.driver._total_jac = None

        start_nruns = model._nruns
        derivs = prob.driver._compute_totals()  # colored

        self.assertEqual(model._nruns - start_nruns, 2)
        cols = [0,2,3,4]
        rows = [1,3,4]
        _check_total_matrix(model, derivs, sparsity[rows, :][:, cols])


class TestFDColoring(TestCSColoring):
    FD_METHOD = 'fd'


class TestCSStaticColoring(unittest.TestCase):
    FD_METHOD = 'cs'

    def setUp(self):
        self.startdir = os.getcwd()
        self.tempdir = tempfile.mkdtemp(prefix=self.__class__.__name__ + '_')
        os.chdir(self.tempdir)

    def tearDown(self):
        os.chdir(self.startdir)
        try:
            shutil.rmtree(self.tempdir)
        except OSError:
            pass

    def test_simple_partials_explicit_static(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        sparsity = np.array(
                [[1, 0, 0, 1, 1, 1, 0],
                 [0, 1, 0, 1, 0, 1, 1],
                 [0, 1, 0, 1, 1, 1, 0],
                 [1, 0, 0, 0, 0, 1, 0],
                 [0, 1, 1, 0, 1, 1, 1]], dtype=float
            )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.random.random(4))
        indeps.add_output('x1', np.random.random(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = comp.compute_approx_coloring(wrt='x*', method=self.FD_METHOD)
        comp._save_coloring(coloring)

        # now make a second problem to use the coloring
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model
        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        comp.declare_coloring(wrt='*', method=self.FD_METHOD)
        comp.use_fixed_coloring()
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        comp._linearize()
        self.assertEqual(comp._nruns - start_nruns, 5)
        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity)

    def test_simple_partials_explicit_shape_bug(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        # create sparsity with last row and col all zeros.
        # bug happened when we created a COO matrix without supplying shape
        sparsity = np.array(
                [[1, 0, 0, 1, 1, 1, 0],
                 [0, 1, 0, 1, 0, 1, 0],
                 [0, 1, 0, 1, 1, 1, 0],
                 [1, 0, 0, 0, 0, 1, 0],
                 [0, 0, 0, 0, 0, 0, 0]], dtype=float
            )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.random.random(4))
        indeps.add_output('x1', np.random.random(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = comp.compute_approx_coloring(wrt='x*', method=self.FD_METHOD)
        comp._save_coloring(coloring)

        # now make a second problem to use the coloring
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model
        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        comp.declare_coloring(wrt='*', method=self.FD_METHOD)
        comp.use_fixed_coloring()
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        comp._linearize()

    def test_simple_partials_implicit_static(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        sparsity = np.array(
            [[1, 0, 0, 1, 1, 1, 0],
             [0, 1, 0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1, 1, 0],
             [1, 0, 0, 0, 0, 1, 0],
             [0, 1, 1, 0, 1, 1, 1]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = comp.compute_approx_coloring(wrt='x*', method=self.FD_METHOD)
        comp._save_coloring(coloring)

        # now create a new problem and set the static coloring
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        comp.declare_coloring(wrt='x*', method=self.FD_METHOD)
        comp.use_fixed_coloring()
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        comp._linearize()
        # add 5 to number of runs to cover the 5 uncolored output columns
        self.assertEqual(comp._nruns - start_nruns, sparsity.shape[0] + 5)
        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity)

    def test_simple_semitotals_static(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        sub = model.add_subsystem('sub', Group())
        comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'sub.comp.x0')
        model.connect('indeps.x1', 'sub.comp.x1')

        model.sub.comp.add_constraint('y0')
        model.sub.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = sub.compute_approx_coloring(wrt='*', method=self.FD_METHOD)
        sub._save_coloring(coloring)

        # now create a second problem and use the static coloring
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        sub = model.add_subsystem('sub', Group())
        comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'sub.comp.x0')
        model.connect('indeps.x1', 'sub.comp.x1')

        model.sub.comp.add_constraint('y0')
        model.sub.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')

        sub.declare_coloring(wrt='*', method=self.FD_METHOD)
        sub.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        derivs = prob.driver._compute_totals()

        nruns = comp._nruns - start_nruns
        self.assertEqual(nruns, 3)
        _check_partial_matrix(sub, sub._jacobian._subjacs_info, sparsity)

    def test_simple_totals_static(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0')
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        model._save_coloring(compute_total_coloring(prob))

        # new Problem, loading the coloring we just computed
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0')
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        model.declare_coloring(wrt='*', method=self.FD_METHOD)
        model.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        derivs = prob.driver._compute_totals()

        nruns = comp._nruns - start_nruns
        self.assertEqual(nruns, 3)
        _check_total_matrix(model, derivs, sparsity)

    def test_totals_over_implicit_comp(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.nonlinear_solver = NonlinearBlockGS()
        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0')
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        model._save_coloring(compute_total_coloring(prob))

        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.nonlinear_solver = NonlinearBlockGS()
        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0')
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        model.declare_coloring(wrt='*', method=self.FD_METHOD)
        model.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        derivs = prob.driver._compute_totals()  # colored

        nruns = comp._nruns - start_nruns
        self.assertEqual(nruns, 3 * 2)
        _check_total_matrix(model, derivs, sparsity)

    def test_totals_of_indices(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0', indices=[0,2])
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        model._save_coloring(compute_total_coloring(prob))


        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0', indices=[0,2])
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        model.declare_coloring(wrt='*', method=self.FD_METHOD)
        model.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        derivs = prob.driver._compute_totals()  # colored

        nruns = comp._nruns - start_nruns
        self.assertEqual(nruns, 3)
        rows = [0,2,3,4]
        _check_total_matrix(model, derivs, sparsity[rows, :])

    def test_totals_wrt_indices(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0')
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0', indices=[0,2])
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        model._save_coloring(compute_total_coloring(prob))


        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                                  isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0')
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0', indices=[0,2])
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        model.declare_coloring(wrt='*', method=self.FD_METHOD)
        model.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        derivs = prob.driver._compute_totals()  # colored

        nruns = comp._nruns - start_nruns
        # only 4 cols to solve for, but we get coloring of [[2],[3],[0,1]] so only 1 better
        self.assertEqual(nruns, 3)
        cols = [0,2,3,4]
        _check_total_matrix(model, derivs, sparsity[:, cols])

    def test_totals_of_wrt_indices(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2))
        # model.declare_coloring('*', method=self.FD_METHOD)
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0', indices=[0,2])
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0', indices=[0,2])
        model.add_design_var('indeps.x1')

        model.approx_totals(method=self.FD_METHOD)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        model._save_coloring(compute_total_coloring(prob))


        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group(dynamic_derivs_repeats=1)

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                                  isplit=2, osplit=2))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_constraint('y0', indices=[0,2])
        model.comp.add_constraint('y1')
        model.add_design_var('indeps.x0', indices=[0,2])
        model.add_design_var('indeps.x1')
        model.approx_totals(method=self.FD_METHOD)

        model.declare_coloring(wrt='*', method=self.FD_METHOD)
        model.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        derivs = prob.driver._compute_totals()  # colored

        nruns = comp._nruns - start_nruns
        self.assertEqual(nruns, 3)
        cols = rows = [0,2,3,4]
        _check_total_matrix(model, derivs, sparsity[rows, :][:, cols])


class TestFDStaticColoring(TestCSStaticColoring):
    FD_METHOD = 'fd'


@unittest.skipUnless(MPI and PETScVector, "MPI and PETSc is required.")
class TestStaticColoringParallelCS(unittest.TestCase):
    N_PROCS = 2
    FD_METHOD = 'cs'

    def setUp(self):
        self.startdir = os.getcwd()
        if MPI.COMM_WORLD.rank == 0:
            self.tempdir = tempfile.mkdtemp(prefix=self.__class__.__name__ + '_')
            MPI.COMM_WORLD.bcast(self.tempdir, root=0)
        else:
            self.tempdir = MPI.COMM_WORLD.bcast(None, root=0)
        os.chdir(self.tempdir)

    def tearDown(self):
        os.chdir(self.startdir)
        if MPI.COMM_WORLD.rank == 0:
            try:
                shutil.rmtree(self.tempdir)
            except OSError:
                pass

    def test_simple_semitotals_all_local_vars(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        sparsity = np.array(
            [[1, 0, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1],
             [1, 0, 0, 0, 0],
             [0, 1, 1, 0, 0]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        sub = model.add_subsystem('sub', Group(num_par_fd=self.N_PROCS))
        sub.approx_totals(method=self.FD_METHOD)
        comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'sub.comp.x0')
        model.connect('indeps.x1', 'sub.comp.x1')

        model.sub.comp.add_constraint('y0')
        model.sub.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = sub.compute_approx_coloring(wrt='*', method=self.FD_METHOD)
        sub._save_coloring(coloring)

        # make sure coloring file exists by the time we try to load the spec
        MPI.COMM_WORLD.barrier()

        # now create a second problem and use the static coloring
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model = Group()

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(3))
        indeps.add_output('x1', np.ones(2))

        model.add_subsystem('indeps', indeps)
        sub = model.add_subsystem('sub', Group(num_par_fd=self.N_PROCS))
        #sub.approx_totals(method=self.FD_METHOD)
        comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD, isplit=2, osplit=2))
        model.connect('indeps.x0', 'sub.comp.x0')
        model.connect('indeps.x1', 'sub.comp.x1')

        model.sub.comp.add_constraint('y0')
        model.sub.comp.add_constraint('y1')
        model.add_design_var('indeps.x0')
        model.add_design_var('indeps.x1')

        sub.declare_coloring(wrt='*', method=self.FD_METHOD)
        sub.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        #derivs = prob.driver._compute_totals()
        sub._linearize(sub._jacobian)
        nruns = comp._nruns - start_nruns
        if sub._full_comm is not None:
            nruns = sub._full_comm.allreduce(nruns)

        _check_partial_matrix(sub, sub._jacobian._subjacs_info, sparsity)
        self.assertEqual(nruns, 3)

    def test_simple_partials_implicit(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        sparsity = np.array(
            [[1, 0, 0, 1, 1, 1, 0],
             [0, 1, 0, 1, 0, 1, 1],
             [0, 1, 0, 1, 1, 1, 0],
             [1, 0, 0, 0, 0, 1, 0],
             [0, 1, 1, 0, 1, 1, 1]], dtype=float
        )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2,
                                                              num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = comp.compute_approx_coloring(wrt='x*', method=self.FD_METHOD)
        comp._save_coloring(coloring)

        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, self.FD_METHOD,
                                                                  isplit=2, osplit=2,
                                                                  num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        # make sure coloring file exists by the time we try to load the spec
        MPI.COMM_WORLD.barrier()

        comp.declare_coloring(wrt='*', method=self.FD_METHOD)
        comp.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        comp._linearize()   # colored
        # number of runs = ncolors + number of outputs (only input columns were colored here)
        nruns = comp._nruns - start_nruns
        if comp._full_comm:
            nruns = comp._full_comm.allreduce(nruns)
        self.assertEqual(nruns, 5 + sparsity.shape[0])

        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity)

    def test_simple_partials_explicit(self):
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        sparsity = np.array(
                [[1, 0, 0, 1, 1, 1, 0],
                 [0, 1, 0, 1, 0, 1, 1],
                 [0, 1, 0, 1, 1, 1, 0],
                 [1, 0, 0, 0, 0, 1, 0],
                 [0, 1, 1, 0, 1, 1, 1]], dtype=float
            )

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                              isplit=2, osplit=2,
                                                              num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = comp.compute_approx_coloring(wrt='x*', method=self.FD_METHOD)
        comp._save_coloring(coloring)

        # now create a new problem and use the previously generated coloring
        prob = Problem(coloring_dir=self.tempdir)
        model = prob.model

        indeps = IndepVarComp()
        indeps.add_output('x0', np.ones(4))
        indeps.add_output('x1', np.ones(3))

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, self.FD_METHOD,
                                                                  isplit=2, osplit=2,
                                                                  num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        # make sure coloring file exists by the time we try to load the spec
        MPI.COMM_WORLD.barrier()

        comp.declare_coloring(wrt='*', method=self.FD_METHOD)
        comp.use_fixed_coloring()
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        comp._linearize()
        nruns = comp._nruns - start_nruns
        if comp._full_comm:
            nruns = comp._full_comm.allreduce(nruns)
        self.assertEqual(nruns, 5)

        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity)


class TestStaticColoringParallelFD(TestStaticColoringParallelCS):
    FD_METHOD = 'fd'


if __name__ == '__main__':
    unitest.main()
