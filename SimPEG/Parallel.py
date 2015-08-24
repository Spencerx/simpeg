from ipyparallel import Client, parallel, Reference, require, depend, interactive
from SimPEG.Utils import CommonReducer
import numpy as np
import networkx

DEFAULT_MPI = True
MPI_BELLWETHERS = ['PMI_SIZE', 'OMPI_UNIVERSE_SIZE']

class SuperReference(object):
    '''
    Object that can be called to return a reference, but
    will only be schedulable on the correct worker(s) if
    its 'lrank' parameter has been set.
    '''

    def __init__(self, ref, lrank=None):

        if (lrank is None) or (type(lrank) is list):
            self.rank = lrank
        else:
            self.rank = [lrank]
            
        self.ref = ref

    def __call__(self, *args, **kwargs):

        from ipyparallel import depend
        from ipyparallel.error import UnmetDependency

        if (self.rank is not None) and (globals().get('rank', None) not in self.rank):
            raise UnmetDependency('Global \'rank\' does not satisfy requirements')

        return self.ref(*args, **kwargs)

class Endpoint(object):
    '''
    Object that holds the namespace of the SimPEG parallel
    footprint on the remote workers.
    '''

    problemFactory = lambda: None   # Callable for constructing system / problem
    surveyFactory = lambda: None    # Callable for constructing survey
    localFields = {}                # Dictionary for storing local fields
    globalFields = {}               # Dictionary for storing merged fields
    localProblems = {}              # Dictionary of local subsystem / problem objects
    localSurveys = {}               # Dictionary of local survey objects
    functions = {}                  # Dictionary of callables to carry out modelling / etc.
    fieldspec = None                # Dictionary of callables to setup field storage objects
    baseSystemConfig = {}           # Base configuration for system

    def setupLocalFields(self, whichfields=None):

        # If no names are specified, clear all fields first
        if whichfields is None:
            self.localFields = {}

        # If we have a 'fieldspec' object...
        if getattr(self, 'fieldspec', None) is not None:
            # ...either loop over the specified names, or all the fields...
            for fn in (whichfields or self.fieldspec):
                # ...and construct a new empty object per the 'fieldspec' constructor.
                self.localFields[fn] = self.fieldspec[fn]()

    def setupLocalSurveys(self, subConfigs):

        # Loop over possible survey configurations (may differ in source terms, etc.)
        for isub in subConfigs:
            # For each 'isub' create a separate copy of the base configuration...
            geom = self.baseSystemConfig['geom'].copy()
            # ...and update with any differences...
            geom.update(subConfigs[isub])
            # ...then construct the Survey object and store it for later pairing.
            self.localSurveys[isub] = self.surveyFactory(geom)

    def setupLocalProblem(self, subConfig):

        # Make a copy w/o the geometry information (which is used by Survey)
        systemConfig = {key: self.baseSystemConfig[key] for key in self.baseSystemConfig if key not in ['geom']}

        # Update with the configuration for this subproblem
        systemConfig.update(subConfig)

        # Create the local subproblem...
        problem = self.problemFactory(systemConfig)
        # ...and pair it with a corresponding survey for this 'isub' (e.g., frequency)...
        problem.pair(self.localSurveys[subConfig['isub']])
        # ...then store in the Endpoint for later access by the scheduler.
        self.localProblems[subConfig['tag']] = problem


class SystemGraph(networkx.DiGraph):
    '''
    NetworkX Directed Graph subclass that knows about
    job status information, and can return a representation
    of itself for use in interactive debugging/testing.
    '''

    @staticmethod
    def _codeStatus(data):
        
        status = 0
        
        if 'jobs' in data:
            status = 1 * data['jobs'][-1].ready() + 1
            if status > 1:
                status += 1 * (not data['jobs'][-1].successful())
        
        return status 

    def _codeGraph(self):
        from networkx.readwrite import json_graph
        
        G = networkx.DiGraph()
        
        for e in self.edges_iter():
            G.add_edge(e[0], e[1])
        
        for n, data in self.nodes_iter(data=True):
            G.add_node(n, status=self._codeStatus(data))
        
        return json_graph.node_link_data(G)

    def RenderHTML(self):
        import pkg_resources
        from IPython.core import display
        import time

        data = str(self._codeGraph())
        uniqueID = hash(time.time())

        formatstr = {
            'uniqueID': 'Graph%s'%uniqueID,
            'JSONData': data,
        }

        code = pkg_resources.resource_string('SimPEG', 'Resources/Parallel/SystemGraph.html')%formatstr

        return display.HTML(data=code)._repr_html_()

try:
    get_ipython().display_formatter.formatters['text/html'].for_type(SystemGraph, SystemGraph.RenderHTML)
except NameError:
    pass

class SystemSolver(object):

    def __init__(self, problem, schedule):

        self.problem = problem
        self.remote = problem.remote
        self.schedule = schedule

    def __call__(self, entry, isrcs):
        
        # TODO: Replace with SuperReference instances
        fnformat = '%s.functions["%s"]'
        fnRef = Reference(fnformat%(self.remote.endpointName, self.schedule[entry]['solve']))
        clearRef = Reference(fnformat%(self.remote.endpointName, self.schedule[entry]['clear']))
        reduceLabels = self.schedule[entry]['reduce']

        dview = self.problem.remote.dview
        lview = self.problem.remote.lview

        chunksPerWorker = getattr(self.problem, 'chunksPerWorker', 1)

        G = SystemGraph()

        mainNode = 'Beginning'
        G.add_node(mainNode)

        # Parse sources
        # TODO: Get from Survey somehow?
        nsrc = self.problem.nsrc
        if isrcs is None:
            isrcs = slice(None)

        elif not isinstance(isrcs, slice):
            raise Exception('Scheduler must run over slice or None!')

        # TODO: Replace w/ hook into Endpoint classes
        systemsOnWorkers = dview['%s.localProblems.keys()'%self.remote.endpointName]
        ids = dview['rank']
        tags = set()
        for ltags in systemsOnWorkers:
            tags = tags.union(set(ltags))

        clearJobs = []
        endNodes = {}
        tailNodes = []

        for tag in tags:

            tagNode = 'Head: %d, %d'%tag
            G.add_edge(mainNode, tagNode)

            relIDs = []
            for i in xrange(len(ids)):

                systems = systemsOnWorkers[i]
                rank = ids[i]

                if tag in systems:
                    relIDs.append(rank)

            systemJobs = []
            endNodes[tag] = []
            systemNodes = []

            with lview.temp_flags(block=False):
                iworks = 0
                for work in self._subSlice(isrcs, int(round(chunksPerWorker*len(relIDs)))):
                    if work:
                        job = lview.apply(fnRef, Reference(self.remote.endpointName), tag, work)
                        systemJobs.append(job)
                        label = 'Compute: %d, %d, %d'%(tag[0], tag[1], iworks)
                        systemNodes.append(label)
                        G.add_node(label, jobs=[job], subslice=work, tag=tag)
                        G.add_edge(tagNode, label)
                        iworks += 1

            if getattr(self.problem, 'ensembleClear', False): # True for ensemble ending, False for individual ending
                tagNode = 'Wrap: %d, %d'%tag
                for label in systemNodes:
                    G.add_edge(label, tagNode)

                for rank in relIDs:

                    with lview.temp_flags(block=False, after=systemJobs):
                        # TODO: Remove dependency on self._hasSystemRank, once the SuperReferences
                        #       are able to be used. They will automatically schedule only on the
                        #       correct (allowed) systems.
                        job = lview.apply(clearRef, Reference(self.remote.endpointName), tag, rank)
                        clearJobs.append(job)
                        label = 'Wrap: %d, %d, %d'%(tag[0],tag[1], rank)
                        G.add_node(label, jobs=[job], tag=tag, rank=rank)
                        endNodes[tag].append(label)
                        G.add_edge(tagNode, label)
            else:

                for i, sjob in enumerate(systemJobs):
                    with lview.temp_flags(block=False, follow=sjob, after=sjob):
                        job = lview.apply(clearRef, Reference(self.remote.endpointName), tag)
                        clearJobs.append(job)
                        label = 'Wrap: %d, %d, %d'%(tag[0],tag[1],i)
                        G.add_node(label, jobs=[job])
                        endNodes[tag].append(label)
                        G.add_edge(systemNodes[i], label)

            tagNode = 'Tail: %d, %d'%tag
            for label in endNodes[tag]:
                G.add_edge(label, tagNode)
            tailNodes.append(tagNode)

        endNode = 'End'
        jobs = []
        after = clearJobs
        for label in reduceLabels:
            job = self.problem.remote.reduceLB(Reference(self.remote.endpointName), label, after)
            after = job
            if job is not None:
                jobs.append(job)
        G.add_node(endNode, jobs=jobs)
        for node in tailNodes:
            G.add_edge(node, endNode)

        return G

    def wait(self, G):
        self.problem.remote.lview.wait(G.node['End']['jobs'] if G.node['End']['jobs'] else (G.node[wn]['jobs'] for wn in (G.predecessors(tn)[0] for tn in G.predecessors('End'))))

    # TODO: Hopefully obsoleted by SuperReference
    @staticmethod
    @interactive
    def _hasSystemRank(endpoint, tag, wid):
        global rank

        return (tag in endpoint.localProblems) and (rank == wid)

    @staticmethod
    def _getChunks(problems, chunks=1):
        nproblems = len(problems)
        return (problems[i*nproblems // chunks: (i+1)*nproblems // chunks] for i in range(chunks))

    @staticmethod
    def _subSlice(insl,  chunks=1):
        start = insl.start or 0
        nproblems = insl.stop - start
        return [slice(start + i*nproblems/chunks, start + (i+1)*nproblems/chunks) for i in xrange(chunks)]


class RemoteInterface(object):

    def __init__(self, profile=None, MPI=None, nThreads=1, bootstrap=None, endpointName='endpoint'):

        # TODO: Add interface for namespace bootstrapping from
        #       the dispatcher / problem side

        if profile is not None:
            pupdate = {'profile': profile}
        else:
            pupdate = {}

        pclient = Client(**pupdate)

        if not self._cdSame(pclient):
            print('Could not change all workers to the same directory as the client!')

        dview = pclient[:]
        dview.block = True
        dview.clear()

        remoteSetup = '''
        import os'''

        parMPISetup = ''' 
        from mpi4py import MPI
        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()''' 

        for command in remoteSetup.strip().split('\n'):
            dview.execute(command.strip())

        dview.scatter('rank', pclient.ids, flatten=True)

        self.e0 = pclient[0]
        self.e0.block = True

        self.useMPI = False
        MPI = DEFAULT_MPI if MPI is None else MPI
        if MPI:
            MPISafe = False

            for var in MPI_BELLWETHERS:
                MPISafe = MPISafe or all(dview['os.getenv("%s")'%(var,)])

            if MPISafe:
                for command in parMPISetup.strip().split('\n'):
                    dview.execute(command.strip())
                ranks = dview['rank']
                reorder = [ranks.index(i) for i in xrange(len(ranks))]
                dview = pclient[reorder]
                dview.block = True
                dview.activate()

                # Set up necessary parts for broadcast-based communication
                self.e0 = pclient[reorder[0]]
                self.e0.block = True
                self.comm = Reference('comm')

            self.useMPI = MPISafe

        self.pclient = pclient
        self.dview = dview
        self.lview = pclient.load_balanced_view()

        self.nThreads = nThreads

        if bootstrap is not None:
            for command in bootstrap.strip().split('\n'):
                dview.execute(command.strip())

        self.endpointName = endpointName

    @property
    def nThreads(self):
        return self._nThreads
    @nThreads.setter
    def nThreads(self, value):
        self._nThreads = value
        self.dview.apply(self._adjustMKLVectorization, self._nThreads)
    

    def __setitem__(self, key, item):

        if self.useMPI:
            self.e0[key] = item
            code = 'if rank != 0: %(key)s = None\n%(key)s = comm.bcast(%(key)s, root=0)'
            self.dview.execute(code%{'key': key})

        else:
            self.dview[key] = item

    def __getitem__(self, key):

        if self.useMPI:
            code = 'temp_%(key)s = None\ntemp_%(key)s = comm.gather(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': key, 'root': 0})
            item = self.e0['temp_%s'%(key,)]
            self.e0.execute('del temp_%s'%(key,))

        else:
            item = self.dview[key]

        return item

    def reduceLB(self, endpoint, key, after=None):

        repeat = lambda value: (value for i in xrange(len(self.pclient.ids)))

        if self.useMPI:
            with self.lview.temp_flags(block=False, after=after):
                job = self.lview.map(self._reduceJob, xrange(len(self.pclient.ids)), repeat(0), repeat(endpoint), repeat(key))

            return job

    def reduce(self, key, axis=None):

        if self.useMPI:
            code = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': key, 'root': 0})

            # if axis is not None:
            #     code = 'temp_%(key)s = temp_%(key)s.sum(axis=%(axis)d)'
            #     self.e0.execute(code%{'key': key, 'axis': axis})

            item = self.e0['temp_%s'%(key,)]
            self.dview.execute('del temp_%s'%(key,))

        else:
            item = reduce(np.add, self.dview[key])

        return item

    def reduceMul(self, key1, key2, axis=None):

        if self.useMPI:
            # Gather
            code_reduce = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            self.dview.execute(code_reduce%{'key': key1, 'root': 0})
            self.dview.execute(code_reduce%{'key': key2, 'root': 0})

            # Multiply
            code_mul = 'temp_%(key1)s%(key2)s = temp_%(key1)s * temp_%(key2)s'
            self.e0.execute(code_mul%{'key1': key1, 'key2': key2})

            # Potentially sum
            if axis is not None:
                code = 'temp_%(key1)s%(key2)s = temp_%(key1)s%(key2)s.sum(axis=%(axis)d)'
                self.e0.execute(code%{'key1': key1, 'key2': key2, 'axis': axis})

            # Pull
            item = self.e0['temp_%(key1)s%(key2)s'%{'key1': key1, 'key2': key2}]

            # Clear
            self.dview.execute('del temp_%s'%(key1,))
            self.dview.execute('del temp_%s'%(key2,))
            self.e0.execute('del temp_%(key1)s%(key2)s'%{'key1': key1, 'key2': key2})

        else:
            item1 = reduce(np.add, self.dview[key1])
            item2 = reduce(np.add, self.dview[key2])
            item = item1 * item2

        return item

    def remoteMulE0(self, key1, key2, axis=None):

        code_mul = 'temp_field = %(key1)s * %(key2)s'
        self.e0.execute(code_mul%{'key1': key1, 'key2': key2})

        if axis is not None:
            code = 'temp_field = temp_field.sum(axis=%(axis)d)'
            self.e0.execute(code%{'axis': axis})

        item = self.e0['temp_field']
        self.e0.execute('del temp_field')

        return item

    def remoteDifference(self, key1, key2, keyresult):

        if self.useMPI:

            root = 0

            # Gather
            code_reduce = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            self.dview.execute(code_reduce%{'key': key1, 'root': root})
            self.dview.execute(code_reduce%{'key': key2, 'root': root})

            # Difference
            code_difference = '%(keyresult)s = temp_%(key1)s - temp_%(key2)s'
            self.e0.execute(code_difference%{'key1': key1, 'key2': key2, 'keyresult': keyresult})

            # Broadcast
            code = 'if rank != 0: %(key)s = None\n%(key)s = comm.bcast(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': keyresult, 'root': root})

            # Clear
            self.e0.execute('del temp_%s'%(key1,))
            self.e0.execute('del temp_%s'%(key2,))

        else:
            item1 = reduce(np.add, self.dview[key1])
            item2 = reduce(np.add, self.dview[key2])

            item = item1 - item2
            self.dview[keyresult] = item

    def remoteOpGatherFirst(self, op, key1, key2, keyresult):

        if self.useMPI:

            root = 0

            # Gather
            code_reduce = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            self.dview.execute(code_reduce%{'key': key1, 'root': root})

            # Difference
            code_difference = '%(keyresult)s = temp_%(key1)s %(op)s %(key2)s'
            self.e0.execute(code_difference%{'op': op, 'key1': key1, 'key2': key2, 'keyresult': keyresult})

            # Broadcast
            code = 'if rank != 0: %(key)s = None\n%(key)s = comm.bcast(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': keyresult, 'root': root})

            # Clear
            self.e0.execute('del temp_%s'%(key1,))

        else:
            item1 = reduce(np.add, self.dview[key1])
            item2 = self.e0[key2] # Assumes that any arbitrary worker has this information

            item = eval('item1 %s item2'%(op,))
            self.dview[keyresult] = item

    def remoteDifferenceGatherFirst(self, *args):
        self.remoteOpGatherFirst('-', *args)

    def remoteSrcEstGatherFirst(self, keyresult, key1, key2, individual=False):

        if self.useMPI:

            root = 0

            # # Gather
            # code_reduce = 'temp_%(key)s = comm.reduce(%(key)s, root=%(root)d)'
            # self.dview.execute(code_reduce%{'key': key1, 'root': root})

            # SrcEst
            if individual:
                code_srcest = '%(keyresult)s = (%(key2)s.conj() * %(key1)s).sum(axis=1) / (%(key1)s.conj() * %(key1)s).sum(axis=1)'
            else:
                code_srcest = '%(keyresult)s = (%(key2)s.conj() * %(key1)s).sum() / (%(key1)s.conj() * %(key1)s).sum()'
            self.e0.execute(code_srcest%{'key1': key1, 'key2': key2, 'keyresult': keyresult})

            # Broadcast
            code = 'if rank != %(root)d: %(key)s = None\n%(key)s = comm.bcast(%(key)s, root=%(root)d)'
            self.dview.execute(code%{'key': keyresult, 'root': root})

        else:

            item1 = reduce(np.add, self.dview[key1])
            item2 = self.e0[key2]

            if individual:
                item = (item2.conj() * item1).sum(axis=1) / (item1.conj() * item1).sum(axis=1)
            else:
                item = (item2.conj() * item1).sum() / (item1.conj() * item1).sum()

            self.dview[keyresult] = item

    def remoteApplySrc(self, keyData, keySrc):

        code = '%(keyData)s = %(keySrc)s * %(keyData)s'
        self.dview.execute(code%{'keyData': keyData, 'keySrc': keySrc})

    # def normFromDifference(self, key):

    #     code = 'temp_norm%(key)s = (%(key)s * %(key)s.conj()).sum(0).sum(0)'
    #     self.e0.execute(code%{'key': key})
    #     code = 'temp_norm%(key)s = {key: np.sqrt(temp_norm%(key)s[key]).real for key in temp_norm%(key)s.keys()}'
    #     self.e0.execute(code%{'key': key})
    #     result = CommonReducer(self.e0['temp_norm%s'%(key,)])
    #     self.e0.execute('del temp_norm%s'%(key,))

    #     return result

    def normFromDifference(self, key):

        code = 'temp_norm = (%(key)s * %(key)s.conj()).sum(0).sum(0)'
        self.e0.execute(code%{'key': key})
        code = 'temp_norm = {key: np.sqrt(temp_norm[key]).real for key in temp_norm}'
        self.e0.execute(code%{'key': key})
        result = CommonReducer(self.e0['temp_norm'])
        self.e0.execute('del temp_norm')

        return result

    @staticmethod
    @interactive
    def _reduceJob(worker, root, endpoint, key):

        from ipyparallel.error import UnmetDependency
        if not rank == worker:
            raise UnmetDependency

        # code = '%(endpoint)s.globalFields["%(key)s"] = comm.reduce(%(endpoint)s.localFields["%(key)s"], root=%(root)d)'
        # exec(code%{'endpoint': endpoint, 'key': key, 'root': root})

        if key not in endpoint.localFields:
            endpoint.localFields[key] = endpoint.fieldspec[key]()

        endpoint.globalFields[key] = comm.reduce(endpoint.localFields[key], root=root)

    @staticmethod
    def _adjustMKLVectorization(nt=1):
        try:
            import mkl
            mkl.set_num_threads(nt)
        except ImportError:
            pass

    @staticmethod
    def _cdSame(rc):
        import os

        dview = rc[:]

        home = os.getenv('HOME')
        cwd = os.getcwd()

        @interactive
        def cdrel(relpath):
            import os
            home = os.getenv('HOME')
            fullpath = os.path.join(home, relpath)
            try:
                os.chdir(fullpath)
            except OSError:
                return False
            else:
                return True

        if cwd.find(home) == 0:
            relpath = cwd[len(home)+1:]
            return all(rc[:].apply_sync(cdrel, relpath))
