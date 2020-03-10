# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2010-2020 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

"""
Logic tree parser, verifier and processor. See specs at
https://blueprints.launchpad.net/openquake-old/+spec/openquake-logic-tree-module

A logic tree object must be iterable and yielding realizations, i.e. objects
with attributes `value`, `weight`, `lt_path` and `ordinal`.
"""

import io
import os
import re
import copy
import time
import logging
import functools
import itertools
import collections
import operator
from collections import namedtuple
import toml
import numpy
from openquake.baselib import hdf5
from openquake.baselib.node import node_from_elem, Node as N, context
from openquake.baselib.general import (groupby, group_array, duplicated,
                                       add_defaults)
import openquake.hazardlib.source as ohs
from openquake.hazardlib.gsim.mgmpe.avg_gmpe import AvgGMPE
from openquake.hazardlib.gsim.base import CoeffsTable
from openquake.hazardlib.imt import from_string
from openquake.hazardlib import valid, nrml, InvalidFile, pmf
from openquake.hazardlib.sourceconverter import SourceGroup
from openquake.commonlib.lt import (
    LogicTreeError, parse_uncertainty, apply_uncertainty)

#: Minimum value for a seed number
MIN_SINT_32 = -(2 ** 31)
#: Maximum value for a seed number
MAX_SINT_32 = (2 ** 31) - 1

TRT_REGEX = re.compile(r'tectonicRegion="([^"]+?)"')
ID_REGEX = re.compile(r'id="([^"]+?)"')
SOURCE_TYPE_REGEX = re.compile(r'<(\w+Source)\b')


def unique(objects, key=None):
    """
    Raise a ValueError if there is a duplicated object, otherwise
    returns the objects as they are.
    """
    dupl = []
    for obj, group in itertools.groupby(sorted(objects), key):
        if sum(1 for _ in group) > 1:
            dupl.append(obj)
    if dupl:
        raise ValueError('Found duplicates %s' % dupl)
    return objects


class Realization(object):
    """
    Generic Realization object with attributes value, weight, ordinal, lt_path,
    samples and optionally offset.
    """
    def __init__(self, value, weight, ordinal, lt_path, samples, offset=0):
        self.value = value
        self.weight = weight
        self.ordinal = ordinal
        self.lt_path = lt_path
        self.samples = samples
        self.offset = offset

    @property
    def pid(self):
        return '_'.join(self.lt_path)  # path ID

    @property
    def name(self):
        """
        Compact representation for the names
        """
        names = self.value.split()
        if len(names) == 1:
            return names[0]
        elif len(names) == 2:
            return ' '.join(names)
        else:
            return ' '.join([names[0], '...', names[-1]])

    def __repr__(self):
        samples = ', samples=%d' % self.samples if self.samples > 1 else ''
        return '<%s #%d %s, path=%s, weight=%s%s>' % (
            self.__class__.__name__, self.ordinal, self.value,
            '_'.join(self.lt_path), self.weight, samples)


@functools.lru_cache()
def get_effective_rlzs(rlzs):
    """
    Group together realizations with the same path
    and yield the first representative of each group.
    """
    effective = []
    ordinal = 0
    for group in groupby(rlzs, operator.attrgetter('pid')).values():
        rlz = group[0]
        if all(path == '@' for path in rlz.lt_path):  # empty realization
            continue
        effective.append(
            Realization(rlz.value, sum(r.weight for r in group),
                        ordinal, rlz.lt_path, len(group)))
        ordinal += 1
    return effective


def sample(weighted_objects, num_samples, seed):
    """
    Take random samples of a sequence of weighted objects

    :param weighted_objects:
        A finite sequence of objects with a `.weight` attribute.
        The weights must sum up to 1.
    :param num_samples:
        The number of samples to return
    :param seed:
        A random seed
    :return:
        A subsequence of the original sequence with `num_samples` elements
    """
    weights = []
    for obj in weighted_objects:
        w = obj.weight
        if isinstance(obj.weight, float):
            weights.append(w)
        else:
            weights.append(w['weight'])
    numpy.random.seed(seed)
    idxs = numpy.random.choice(len(weights), num_samples, p=weights)
    # NB: returning an array would break things
    return [weighted_objects[idx] for idx in idxs]


class Branch(object):
    """
    Branch object, represents a ``<logicTreeBranch />`` element.

    :param bs_id:
        BranchSetID of the branchset to which the branch belongs
    :param branch_id:
        Value of ``@branchID`` attribute.
    :param weight:
        float value of weight assigned to the branch. A text node contents
        of ``<uncertaintyWeight />`` child node.
    :param value:
        The actual uncertainty parameter value. A text node contents
        of ``<uncertaintyModel />`` child node. Type depends
        on the branchset's uncertainty type.
    """
    def __init__(self, bs_id, branch_id, weight, value):
        self.bs_id = bs_id
        self.branch_id = branch_id
        self.weight = weight
        self.value = value
        self.bset = None

    def __repr__(self):
        if self.bset:
            return '%s%s' % (self.branch_id, self.bset)
        else:
            return '%s' % self.branch_id


def tomldict(ddic):
    out = {}
    for key, dic in ddic.items():
        out[key] = toml.dumps({k: v.strip() for k, v in dic.items()
                               if k != 'uncertaintyType'}).strip()
    return out


class BranchSet(object):
    """
    Branchset object, represents a ``<logicTreeBranchSet />`` element.

    :param uncertainty_type:
        String value. According to the spec one of:

        gmpeModel
            Branches contain references to different GMPEs. Values are parsed
            as strings and are supposed to be one of supported GMPEs. See list
            at :class:`GMPELogicTree`.
        sourceModel
            Branches contain references to different PSHA source models. Values
            are treated as file names, relatively to base path.
        maxMagGRRelative
            Different values to add to Gutenberg-Richter ("GR") maximum
            magnitude. Value should be interpretable as float.
        bGRRelative
            Values to add to GR "b" value. Parsed as float.
        maxMagGRAbsolute
            Values to replace GR maximum magnitude. Values expected to be
            lists of floats separated by space, one float for each GR MFD
            in a target source in order of appearance.
        abGRAbsolute
            Values to replace "a" and "b" values of GR MFD. Lists of pairs
            of floats, one pair for one GR MFD in a target source.
        incrementalMFDAbsolute
            Replaces an evenly discretized MFD with the values provided
        simpleFaultDipRelative
            Increases or decreases the angle of fault dip from that given
            in the original source model
        simpleFaultDipAbsolute
            Replaces the fault dip in the specified source(s)
        simpleFaultGeometryAbsolute
            Replaces the simple fault geometry (trace, upper seismogenic depth
            lower seismogenic depth and dip) of a given source with the values
            provided
        complexFaultGeometryAbsolute
            Replaces the complex fault geometry edges of a given source with
            the values provided
        characteristicFaultGeometryAbsolute
            Replaces the complex fault geometry surface of a given source with
            the values provided

    :param filters:
        Dictionary, a set of filters to specify which sources should
        the uncertainty be applied to. Represented as branchset element's
        attributes in xml:

        applyToSources
            The uncertainty should be applied only to specific sources.
            This filter is required for absolute uncertainties (also
            only one source can be used for those). Value should be the list
            of source ids. Can be used only in source model logic tree.
        applyToSourceType
            Can be used in the source model logic tree definition. Allows
            to specify to which source type (area, point, simple fault,
            complex fault) the uncertainty applies to.
        applyToTectonicRegionType
            Can be used in both the source model and GMPE logic trees. Allows
            to specify to which tectonic region type (Active Shallow Crust,
            Stable Shallow Crust, etc.) the uncertainty applies to. This
            filter is required for all branchsets in GMPE logic tree.
    """
    def __init__(self, uncertainty_type, filters):
        self.branches = []
        self.uncertainty_type = uncertainty_type
        self.filters = filters

    def enumerate_paths(self):
        """
        Generate all possible paths starting from this branch set.

        :returns:
            Generator of two-item tuples. Each tuple contains weight
            of the path (calculated as a product of the weights of all path's
            branches) and list of path's :class:`Branch` objects. Total sum
            of all paths' weights is 1.0
        """
        for path in self._enumerate_paths([]):
            flat_path = []
            weight = 1.0
            while path:
                path, branch = path
                weight *= branch.weight
                flat_path.append(branch)
            yield weight, flat_path[::-1]

    def _enumerate_paths(self, prefix_path):
        """
        Recursive (private) part of :func:`enumerate_paths`. Returns generator
        of recursive lists of two items, where second item is the branch object
        and first one is itself list of two items.
        """
        for branch in self.branches:
            path = [prefix_path, branch]
            if branch.bset is not None:
                yield from branch.bset._enumerate_paths(path)
            else:
                yield path

    def __getitem__(self, branch_id):
        """
        Return :class:`Branch` object belonging to this branch set with id
        equal to ``branch_id``.
        """
        for branch in self.branches:
            if branch.branch_id == branch_id:
                return branch
        raise KeyError(branch_id)

    def filter_source(self, source):
        # pylint: disable=R0911,R0912
        """
        Apply filters to ``source`` and return ``True`` if uncertainty should
        be applied to it.
        """
        for key, value in self.filters.items():
            if key == 'applyToTectonicRegionType':
                if value != source.tectonic_region_type:
                    return False
            elif key == 'applyToSourceType':
                if value == 'area':
                    if not isinstance(source, ohs.AreaSource):
                        return False
                elif value == 'point':
                    # area source extends point source
                    if (not isinstance(source, ohs.PointSource)
                            or isinstance(source, ohs.AreaSource)):
                        return False
                elif value == 'simpleFault':
                    if not isinstance(source, ohs.SimpleFaultSource):
                        return False
                elif value == 'complexFault':
                    if not isinstance(source, ohs.ComplexFaultSource):
                        return False
                elif value == 'characteristicFault':
                    if not isinstance(source, ohs.CharacteristicFaultSource):
                        return False
                else:
                    raise AssertionError("unknown source type '%s'" % value)
            elif key == 'applyToSources':
                if source and source.source_id not in value:
                    return False
            else:
                raise AssertionError("unknown filter '%s'" % key)
        # All filters pass, return True.
        return True

    def __repr__(self):
        return repr(self.branches)


# manage the legacy logicTreeBranchingLevel nodes
def _bsnodes(fname, branchinglevel):
    if branchinglevel.tag.endswith('logicTreeBranchingLevel'):
        if len(branchinglevel) > 1:
            raise InvalidLogicTree(
                '%s: Branching level %s has multiple branchsets'
                % (fname, branchinglevel['branchingLevelID']))
        return branchinglevel.nodes
    elif branchinglevel.tag.endswith('logicTreeBranchSet'):
        return [branchinglevel]
    else:
        raise ValueError('Expected BranchingLevel/BranchSet, got %s' %
                         branchinglevel)


Info = collections.namedtuple('Info', 'smpaths, applytosources')


def collect_info(smlt):
    """
    Given a path to a source model logic tree, collect all of the
    path names to the source models it contains and build
    1. a dictionary source model branch ID -> paths
    2. a dictionary source model branch ID -> source IDs in applyToSources

    :param smlt: source model logic tree file
    :returns: an Info namedtupled containing the two dictionaries
    """
    n = nrml.read(smlt)
    try:
        blevels = n.logicTree
    except Exception:
        raise InvalidFile('%s is not a valid source_model_logic_tree_file'
                          % smlt)
    paths = collections.defaultdict(set)  # branchID -> paths
    applytosources = collections.defaultdict(list)  # branchID -> source IDs
    for blevel in blevels:
        for bset in _bsnodes(smlt, blevel):
            if 'applyToSources' in bset.attrib:
                applytosources[bset.get('applyToBranches')].extend(
                        bset['applyToSources'].split())
            if bset['uncertaintyType'] in 'sourceModel extendModel':
                for br in bset:
                    with context(smlt, br):
                        fnames = unique(br.uncertaintyModel.text.split())
                        paths[br['branchID']].update(_abs_paths(smlt, fnames))
    return Info({k: sorted(v) for k, v in paths.items()}, applytosources)


def _abs_paths(smlt, fnames):
    # relative -> absolute paths
    base_path = os.path.dirname(smlt)
    paths = []
    for fname in fnames:
        if os.path.isabs(fname):
            raise InvalidFile('%s: %s must be a relative path' % (smlt, fname))
        fname = os.path.abspath(os.path.join(base_path, fname))
        if os.path.exists(fname):  # consider only real paths
            paths.append(fname)
    return paths


def read_source_groups(fname):
    """
    :param fname: a path to a source model XML file
    :return: a list of SourceGroup objects containing source nodes
    """
    smodel = nrml.read(fname).sourceModel
    src_groups = []
    if smodel[0].tag.endswith('sourceGroup'):  # NRML 0.5 format
        for sg_node in smodel:
            sg = SourceGroup(sg_node['tectonicRegion'])
            sg.sources = sg_node.nodes
            src_groups.append(sg)
    else:  # NRML 0.4 format: smodel is a list of source nodes
        src_groups.extend(SourceGroup.collect(smodel))
    return src_groups


class SourceModelLogicTree(object):
    """
    Source model logic tree parser.

    :param filename:
        Full pathname of logic tree file
    :raises LogicTreeError:
        If logic tree file has a logic error, which can not be prevented
        by xml schema rules (like referencing sources with missing id).
    """
    _xmlschema = None

    FILTERS = ('applyToTectonicRegionType',
               'applyToSources',
               'applyToSourceType')

    def __init__(self, filename, seed=0, num_samples=0):
        self.filename = filename
        self.basepath = os.path.dirname(filename)
        self.seed = seed
        self.num_samples = num_samples
        self.branches = {}  # branch_id -> branch
        self.bsetdict = {}
        self.previous_branches = []
        self.tectonic_region_types = set()
        self.source_types = set()
        self.root_branchset = None
        root = nrml.read(filename)
        try:
            tree = root.logicTree
        except AttributeError:
            raise LogicTreeError(
                root, self.filename, "missing logicTree node")
        self.parse_tree(tree)

    @property
    def on_each_source(self):
        """
        True if there is an applyToSources for each source.
        """
        return (self.info.applytosources and
                self.info.applytosources == self.source_ids)

    def parse_tree(self, tree_node):
        """
        Parse the whole tree and point ``root_branchset`` attribute
        to the tree's root.
        """
        self.info = collect_info(self.filename)
        self.source_ids = collections.defaultdict(list)
        t0 = time.time()
        for depth, blnode in enumerate(tree_node.nodes):
            [bsnode] = _bsnodes(self.filename, blnode)
            self.parse_branchset(bsnode, depth)
        dt = time.time() - t0
        bname = os.path.basename(self.filename)
        logging.info('Validated %s in %.2f seconds', bname, dt)

    def parse_branchset(self, branchset_node, depth):
        """
        :param branchset_ node:
            ``etree.Element`` object with tag "logicTreeBranchSet".
        :param depth:
            The sequential number of this branching level, based on 0.

        Enumerates children branchsets and call :meth:`parse_branchset`,
        :meth:`validate_branchset`, :meth:`parse_branches` and finally
        :meth:`apply_branchset` for each.

        Keeps track of "open ends" -- the set of branches that don't have
        any child branchset on this step of execution. After processing
        of every branchset only those branches that are listed in it
        can have child branchsets (if there is one on the next level).
        """
        attrs = branchset_node.attrib.copy()
        self.bsetdict[attrs.pop('branchSetID')] = attrs
        uncertainty_type = branchset_node.attrib.get('uncertaintyType')
        filters = dict((filtername, branchset_node.attrib.get(filtername))
                       for filtername in self.FILTERS
                       if filtername in branchset_node.attrib)
        self.validate_filters(branchset_node, uncertainty_type, filters)

        filters = self.parse_filters(branchset_node, uncertainty_type, filters)
        branchset = BranchSet(uncertainty_type, filters)
        self.validate_branchset(branchset_node, depth, branchset)

        self.parse_branches(branchset_node, branchset)
        if self.root_branchset is None:  # not set yet
            self.num_paths = 1
            self.root_branchset = branchset
        else:
            apply_to_branches = branchset_node.attrib.get('applyToBranches')
            if apply_to_branches:
                self.apply_branchset(
                    apply_to_branches, branchset_node.lineno, branchset)
            else:
                for branch in self.previous_branches:
                    branch.bset = branchset
        self.previous_branches = branchset.branches
        self.num_paths *= len(branchset.branches)

    def parse_branches(self, branchset_node, branchset):
        """
        Create and attach branches at ``branchset_node`` to ``branchset``.

        :param branchset_node:
            Same as for :meth:`parse_branchset`.
        :param branchset:
            An instance of :class:`BranchSet`.

        Checks that each branch has :meth:`valid <validate_uncertainty_value>`
        value, unique id and that all branches have total weight of 1.0.

        :return:
            ``None``, all branches are attached to provided branchset.
        """
        bs_id = branchset_node['branchSetID']
        weight_sum = 0
        branches = branchset_node.nodes
        values = []
        for branchnode in branches:
            weight = ~branchnode.uncertaintyWeight
            weight_sum += weight
            value_node = node_from_elem(branchnode.uncertaintyModel)
            if value_node.text is not None:
                values.append(value_node.text.strip())
            if branchset.uncertainty_type in ('sourceModel', 'extendModel'):
                try:
                    for fname in value_node.text.strip().split():
                        if fname.endswith(('.xml', '.nrml')):  # except UCERF
                            self.collect_source_model_data(
                                branchnode['branchID'], fname)
                except Exception as exc:
                    raise LogicTreeError(
                        value_node, self.filename, str(exc)) from exc
            value = parse_uncertainty(branchset.uncertainty_type, value_node,
                                      self.filename)
            branch_id = branchnode.attrib.get('branchID')
            branch = Branch(bs_id, branch_id, weight, value)
            if branch_id in self.branches:
                raise LogicTreeError(
                    branchnode, self.filename,
                    "branchID '%s' is not unique" % branch_id)
            self.branches[branch_id] = branch
            branchset.branches.append(branch)
        if abs(weight_sum - 1.0) > pmf.PRECISION:
            raise LogicTreeError(
                branchset_node, self.filename,
                "branchset weights don't sum up to 1.0")
        if len(set(values)) < len(values):
            raise LogicTreeError(
                branchset_node, self.filename,
                "there are duplicate values in uncertaintyModel: " +
                ' '.join(values))

    def sample_path(self, seed):
        """
        Return a list of branches.

        :param seed: the seed used for the sampling
        """
        branchset = self.root_branchset
        branches = []
        while branchset is not None:
            [branch] = sample(branchset.branches, 1, seed)
            branches.append(branch)
            branchset = branch.bset
        return branches

    def __iter__(self):
        """
        Yield Realization tuples. Notice that the weight is homogeneous when
        sampling is enabled, since it is accounted for in the sampling
        procedure.
        """
        if self.num_samples:
            # random sampling of the logic tree
            weight = 1. / self.num_samples
            for i in range(self.num_samples):
                smlt_path = self.sample_path(self.seed + i)
                name = smlt_path[0].value
                smlt_path_ids = [branch.branch_id for branch in smlt_path]
                yield Realization(name, weight, None, tuple(smlt_path_ids), 1)
        else:  # full enumeration
            ordinal = 0
            for weight, smlt_path in self.root_branchset.enumerate_paths():
                name = smlt_path[0].value
                smlt_branch_ids = [branch.branch_id for branch in smlt_path]
                yield Realization(name, weight, ordinal,
                                  tuple(smlt_branch_ids), 1)
                ordinal += 1

    def parse_filters(self, branchset_node, uncertainty_type, filters):
        """
        See superclass' method for description and signature specification.

        Converts "applyToSources" filter value by just splitting it to a list.
        """
        if 'applyToSources' in filters:
            filters['applyToSources'] = filters['applyToSources'].split()
        return filters

    def validate_filters(self, branchset_node, uncertainty_type, filters):
        """
        See superclass' method for description and signature specification.

        Checks that the following conditions are met:

        * "sourceModel" uncertainties can not have filters.
        * Absolute uncertainties must have only one filter --
          "applyToSources", with only one source id.
        * All other uncertainty types can have either no or one filter.
        * Filter "applyToSources" must mention only source ids that
          exist in source models.
        * Filter "applyToTectonicRegionType" must mention only tectonic
          region types that exist in source models.
        * Filter "applyToSourceType" must mention only source types
          that exist in source models.
        """
        if uncertainty_type == 'sourceModel' and filters:
            raise LogicTreeError(
                branchset_node, self.filename,
                'filters are not allowed on source model uncertainty')

        if len(filters) > 1:
            raise LogicTreeError(
                branchset_node, self.filename,
                "only one filter is allowed per branchset")

        if 'applyToTectonicRegionType' in filters:
            if not filters['applyToTectonicRegionType'] \
                    in self.tectonic_region_types:
                raise LogicTreeError(
                    branchset_node, self.filename,
                    "source models don't define sources of tectonic region "
                    "type '%s'" % filters['applyToTectonicRegionType'])

        if uncertainty_type in ('abGRAbsolute', 'maxMagGRAbsolute',
                                'simpleFaultGeometryAbsolute',
                                'complexFaultGeometryAbsolute'):
            if not filters or not list(filters) == ['applyToSources'] \
                    or not len(filters['applyToSources'].split()) == 1:
                raise LogicTreeError(
                    branchset_node, self.filename,
                    "uncertainty of type '%s' must define 'applyToSources' "
                    "with only one source id" % uncertainty_type)
        if uncertainty_type in ('simpleFaultDipRelative',
                                'simpleFaultDipAbsolute'):
            if not filters or (not ('applyToSources' in filters) and not
                               ('applyToSourceType' in filters)):
                raise LogicTreeError(
                    branchset_node, self.filename,
                    "uncertainty of type '%s' must define either"
                    "'applyToSources' or 'applyToSourceType'"
                    % uncertainty_type)

        if 'applyToSourceType' in filters:
            if not filters['applyToSourceType'] in self.source_types:
                raise LogicTreeError(
                    branchset_node, self.filename,
                    "source models don't define sources of type '%s'" %
                    filters['applyToSourceType'])

        if 'applyToSources' in filters:
            if (len(self.source_ids) > 1 and 'applyToBranches' not in
                    branchset_node.attrib):
                raise LogicTreeError(
                    branchset_node, self.filename, "applyToBranch must be "
                    "specified together with applyToSources")
            for source_id in filters['applyToSources'].split():
                cnt = sum(source_id in source_ids
                          for source_ids in self.source_ids.values())
                if cnt == 0:
                    raise LogicTreeError(
                        branchset_node, self.filename,
                        "source with id '%s' is not defined in source "
                        "models" % source_id)

    def validate_branchset(self, branchset_node, depth, branchset):
        """
        See superclass' method for description and signature specification.

        Checks that the following conditions are met:

        * First branching level must contain exactly one branchset, which
          must be of type "sourceModel".
        * All other branchsets must not be of type "sourceModel"
          or "gmpeModel".
        """
        if depth == 0:
            if branchset.uncertainty_type != 'sourceModel':
                raise LogicTreeError(
                    branchset_node, self.filename,
                    'first branchset must define an uncertainty '
                    'of type "sourceModel"')
        else:
            if branchset.uncertainty_type == 'sourceModel':
                raise LogicTreeError(
                    branchset_node, self.filename,
                    'uncertainty of type "sourceModel" can be defined '
                    'on first branchset only')
            elif branchset.uncertainty_type == 'gmpeModel':
                raise LogicTreeError(
                    branchset_node, self.filename,
                    'uncertainty of type "gmpeModel" is not allowed '
                    'in source model logic tree')

    def apply_branchset(self, apply_to_branches, lineno, branchset):
        """
        See superclass' method for description and signature specification.

        Parses branchset node's attribute ``@applyToBranches`` to apply
        following branchests to preceding branches selectively. Branching
        level can have more than one branchset exactly for this: different
        branchsets can apply to different open ends.

        Checks that branchset tries to be applied only to branches on previous
        branching level which do not have a child branchset yet.
        """
        for branch_id in apply_to_branches.split():
            if branch_id not in self.branches:
                raise LogicTreeError(
                    lineno, self.filename,
                    "branch '%s' is not yet defined" % branch_id)
            branch = self.branches[branch_id]
            if branch.bset is not None:
                raise LogicTreeError(
                    lineno, self.filename,
                    "branch '%s' already has child branchset" % branch_id)
            branch.bset = branchset

    def _get_source_model(self, source_model_file):
        # NB: do not remove this, it is meant to be overridden in the tests
        return open(os.path.join(self.basepath, source_model_file),
                    encoding='utf-8')

    def collect_source_model_data(self, branch_id, source_model):
        """
        Parse source model file and collect information about source ids,
        source types and tectonic region types available in it. That
        information is used then for :meth:`validate_filters` and
        :meth:`validate_uncertainty_value`.
        """
        # using regular expressions is a lot faster than using the
        with self._get_source_model(source_model) as sm:
            xml = sm.read()
        self.tectonic_region_types.update(TRT_REGEX.findall(xml))
        self.source_ids[branch_id].extend(ID_REGEX.findall(xml))
        self.source_types.update(SOURCE_TYPE_REGEX.findall(xml))

    def apply_uncertainties(self, ltpath, fname, converter):
        """
        :param ltpath:
            List of branch IDs
        :param fname:
            Path to a source model file
        :param converter:
            class:`openquake.hazardlib.sourceconverter.SourceConverter` object
        :return:
            A list of source groups
        """
        dirname = os.path.dirname(self.filename)
        [sm] = nrml.read_source_models([fname], converter)
        base_ids = set(src.source_id for sg in sm.src_groups for src in sg)
        src_groups = sm.src_groups
        path = ltpath
        branchset = self.root_branchset
        branchsets_and_uncertainties = []
        while branchset is not None:
            brid, path = path[0], path[1:]
            branch = branchset[brid]
            if branchset.uncertainty_type == 'extendModel':
                extname = os.path.join(dirname, branch.value)
                [ext] = nrml.read_source_models([extname], converter)
                extra_ids = set(src.source_id for sg in ext.src_groups
                                for src in sg)
                common = base_ids & extra_ids
                if common:
                    raise InvalidFile(
                        '%s contains source(s) %s already present in %s' %
                        (extname, common, sm.fname))
                src_groups.extend(ext.src_groups)
            elif branchset.uncertainty_type != 'sourceModel':
                branchsets_and_uncertainties.append(
                    (branchset, branch.value))
            branchset = branch.bset
        if branchsets_and_uncertainties:
            for i, sg in enumerate(src_groups):
                sg.changes = 0
                for source in sg:
                    changes = 0
                    for bset, value in branchsets_and_uncertainties:
                        if bset.filter_source(source):
                            apply_uncertainty(
                                bset.uncertainty_type, source, value)
                            changes += 1
                    if changes:  # redoing count_ruptures can be slow
                        source.num_ruptures = source.count_ruptures()
                        sg.changes += changes
                src_groups[i] = sg
        return src_groups

    def get_trti_eri(self):
        """
        :returns: a function grp_id -> (trti, eri)
        """
        return lambda gid, n=len(get_effective_rlzs(self)): divmod(gid, n)

    def get_grp_id(self, trts):
        """
        :returns: a function trt, eri -> grp_id
        """
        trti = {trt: i for i, trt in enumerate(trts)}
        return (lambda trt, eri, n=len(get_effective_rlzs(self)):
                trti[trt] * n + int(eri))

    def __toh5__(self):
        tbl = []
        for brid, br in self.branches.items():
            dic = self.bsetdict[br.bs_id].copy()
            utype = dic.pop('uncertaintyType')
            tbl.append((br.bs_id, brid, utype, br.value, br.weight))
        dt = [('branchset', hdf5.vstr), ('branch', hdf5.vstr),
              ('utype', hdf5.vstr), ('uvalue', hdf5.vstr), ('weight', float)]
        attrs = tomldict(self.bsetdict)
        attrs['num_samples'] = self.num_samples
        attrs['filename'] = self.filename
        return numpy.array(tbl, dt), attrs

    def __fromh5__(self, array, attrs):
        bsets = []
        for bsid, rows in group_array(array, 'branchset').items():
            utype = rows[0]['utype']
            bset = BranchSet(utype, [])  # TODO: filters
            bset.id = bsid
            for row in rows:
                br = Branch(bsid, row['branch'], row['weight'], row['uvalue'])
                bset.branches.append(br)
            bsets.append(bset)
        self.root_branchset = bsets[0]
        for i, childset in enumerate(bsets[1:]):
            dic = toml.loads(attrs[childset.id])
            atb = dic.get('applyToBranches')
            for branch in bsets[i].branches:  # parent branches
                if not atb or branch.branch_id in atb:
                    branch.bset = bset
        self.num_samples = attrs['num_samples']
        self.filename = attrs['filename']

    def __str__(self):
        return '<%s%s>' % (self.__class__.__name__, repr(self.root_branchset))


# used in GsimLogicTree
BranchTuple = namedtuple('BranchTuple', 'trt id gsim weight effective')


class InvalidLogicTree(Exception):
    pass


class ImtWeight(object):
    """
    A composite weight by IMTs extracted from the gsim_logic_tree_file
    """
    def __init__(self, branch, fname):
        with context(fname, branch.uncertaintyWeight):
            nodes = list(branch.getnodes('uncertaintyWeight'))
            if 'imt' in nodes[0].attrib:
                raise InvalidLogicTree('The first uncertaintyWeight has an imt'
                                       ' attribute')
            self.dic = {'weight': float(nodes[0].text)}
            imts = []
            for n in nodes[1:]:
                self.dic[n['imt']] = float(n.text)
                imts.append(n['imt'])
            if len(set(imts)) < len(imts):
                raise InvalidLogicTree(
                    'There are duplicated IMTs in the weights')

    def __mul__(self, other):
        new = object.__new__(self.__class__)
        if isinstance(other, self.__class__):
            keys = set(self.dic) | set(other.dic)
            new.dic = {k: self[k] * other[k] for k in keys}
        else:  # assume a float
            new.dic = {k: self.dic[k] * other for k in self.dic}
        return new

    __rmul__ = __mul__

    def __add__(self, other):
        new = object.__new__(self.__class__)
        if isinstance(other, self.__class__):
            new.dic = {k: self.dic[k] + other[k] for k in self.dic}
        else:  # assume a float
            new.dic = {k: self.dic[k] + other for k in self.dic}
        return new

    __radd__ = __add__

    def __truediv__(self, other):
        new = object.__new__(self.__class__)
        if isinstance(other, self.__class__):
            new.dic = {k: self.dic[k] / other[k] for k in self.dic}
        else:  # assume a float
            new.dic = {k: self.dic[k] / other for k in self.dic}
        return new

    def is_one(self):
        """
        Check that all the inner weights are 1 up to the precision
        """
        return all(abs(v - 1.) < pmf.PRECISION for v in self.dic.values() if v)

    def __getitem__(self, imt):
        try:
            return self.dic[imt]
        except KeyError:
            return self.dic['weight']

    def __repr__(self):
        return '<%s %s>' % (self.__class__.__name__, self.dic)


class GsimLogicTree(object):
    """
    A GsimLogicTree instance is an iterable yielding `Realization`
    tuples with attributes `value`, `weight` and `lt_path`, where
    `value` is a dictionary {trt: gsim}, `weight` is a number in the
    interval 0..1 and `lt_path` is a tuple with the branch ids of the
    given realization.

    :param str fname:
        full path of the gsim_logic_tree file
    :param tectonic_region_types:
        a sequence of distinct tectonic region types
    :param ltnode:
        usually None, but it can also be a
        :class:`openquake.hazardlib.nrml.Node` object describing the
        GSIM logic tree XML file, to avoid reparsing it
    """
    @classmethod
    def from_(cls, gsim):
        """
        Generate a trivial GsimLogicTree from a single GSIM instance.
        """
        ltbranch = N('logicTreeBranch', {'branchID': 'b1'},
                     nodes=[N('uncertaintyModel', text=str(gsim)),
                            N('uncertaintyWeight', text='1.0')])
        lt = N('logicTree', {'logicTreeID': 'lt1'},
               nodes=[N('logicTreeBranchingLevel', {'branchingLevelID': 'bl1'},
                        nodes=[N('logicTreeBranchSet',
                                 {'applyToTectonicRegionType': '*',
                                  'branchSetID': 'bs1',
                                  'uncertaintyType': 'gmpeModel'},
                                 nodes=[ltbranch])])])
        return cls('fake/' + gsim.__class__.__name__, ['*'], ltnode=lt)

    def __init__(self, fname, tectonic_region_types=['*'], ltnode=None):
        self.filename = fname
        trts = sorted(tectonic_region_types)
        if len(trts) > len(set(trts)):
            raise ValueError(
                'The given tectonic region types are not distinct: %s' %
                ','.join(trts))
        self.values = collections.defaultdict(list)  # {trt: gsims}
        self._ltnode = ltnode or nrml.read(fname).logicTree
        self.bs_id_by_trt = {}
        self.branches = self._build_trts_branches(trts)  # sorted by trt
        if trts != ['*']:
            self.values = {trt: self.values[trt] for trt in trts}
        if tectonic_region_types and not self.branches:
            raise InvalidLogicTree(
                'Could not find branches with attribute '
                "'applyToTectonicRegionType' in %s" %
                set(tectonic_region_types))

    @property
    def req_site_params(self):
        site_params = set()
        for trt in self.values:
            for gsim in self.values[trt]:
                site_params.update(gsim.REQUIRES_SITES_PARAMETERS)
        return site_params

    def check_imts(self, imts):
        """
        Make sure the IMTs are recognized by all GSIMs in the logic tree
        """
        for trt in self.values:
            for gsim in self.values[trt]:
                for attr in dir(gsim):
                    coeffs = getattr(gsim, attr)
                    if not isinstance(coeffs, CoeffsTable):
                        continue
                    for imt in imts:
                        if imt.startswith('SA'):
                            try:
                                coeffs[from_string(imt)]
                            except KeyError:
                                raise ValueError(
                                    '%s is out of the period range defined '
                                    'for %s' % (imt, gsim))

    def __toh5__(self):
        weights = set()
        for branch in self.branches:
            weights.update(branch.weight.dic)
        dt = [('trt', hdf5.vstr), ('branch', hdf5.vstr),
              ('uncertainty', hdf5.vstr)] + [
            (weight, float) for weight in sorted(weights)]
        branches = [(b.trt, b.id, b.gsim) +
                    tuple(b.weight[weight] for weight in sorted(weights))
                    for b in self.branches if b.effective]
        dic = {'branches': numpy.array(branches, dt)}
        if hasattr(self, 'filename'):
            # missing in EventBasedRiskTestCase case_1f
            dirname = os.path.dirname(self.filename)
            for gsims in self.values.values():
                for gsim in gsims:
                    for k, v in gsim.kwargs.items():
                        if k.endswith(('_file', '_table')):
                            fname = os.path.join(dirname, v)
                            with open(fname, 'rb') as f:
                                dic[os.path.basename(v)] = f.read()
        return dic, {}

    def __fromh5__(self, dic, attrs):
        self.branches = []
        self.values = collections.defaultdict(list)
        for branch in dic.pop('branches'):
            br_id = branch['branch']
            gsim = valid.gsim(branch['uncertainty'])
            for k, v in gsim.kwargs.items():
                if k.endswith(('_file', '_table')):
                    arr = numpy.asarray(dic[os.path.basename(v)][()])
                    gsim.kwargs[k] = io.BytesIO(bytes(arr))
            gsim.__init__(**gsim.kwargs)
            self.values[branch['trt']].append(gsim)
            weight = object.__new__(ImtWeight)
            # branch has dtype ('trt', 'branch', 'uncertainty', 'weight', ...)
            weight.dic = {w: branch[w] for w in branch.dtype.names[3:]}
            if len(weight.dic) > 1:
                gsim.weight = weight
            bt = BranchTuple(branch['trt'], br_id, gsim, weight, True)
            self.branches.append(bt)

    def reduce(self, trts):
        """
        Reduce the GsimLogicTree.

        :param trts: a subset of tectonic region types
        :returns: a reduced GsimLogicTree instance
        """
        new = object.__new__(self.__class__)
        vars(new).update(vars(self))
        if trts != {'*'}:
            new.branches = []
            for br in self.branches:
                branch = BranchTuple(br.trt, br.id, br.gsim, br.weight,
                                     br.trt in trts)
                new.branches.append(branch)
        return new

    def collapse(self, branchset_ids):
        """
        Collapse the GsimLogicTree by using AgvGMPE instances if needed

        :param branchset_ids: branchset ids to collapse
        :returns: a collapse GsimLogicTree instance
        """
        new = object.__new__(self.__class__)
        vars(new).update(vars(self))
        new.branches = []
        for trt, grp in itertools.groupby(self.branches, lambda b: b.trt):
            bs_id = self.bs_id_by_trt[trt]
            brs = []
            gsims = []
            weights = []
            for br in grp:
                brs.append(br.id)
                gsims.append(br.gsim)
                weights.append(br.weight)
            if len(gsims) > 1 and bs_id in branchset_ids:
                gsim = object.__new__(AvgGMPE)
                gsim.kwargs = {brid: {gsim.__class__.__name__: gsim.kwargs}
                               for brid, gsim in zip(brs, gsims)}
                gsim.gsims = gsims
                gsim.weights = numpy.array([w['weight'] for w in weights])
                branch = BranchTuple(trt, bs_id, gsim, sum(weights), True)
                new.branches.append(branch)
            else:
                new.branches.append(br)
        return new

    def get_num_branches(self):
        """
        Return the number of effective branches for tectonic region type,
        as a dictionary.
        """
        num = {}
        for trt, branches in itertools.groupby(
                self.branches, operator.attrgetter('trt')):
            num[trt] = sum(1 for br in branches if br.effective)
        return num

    def get_num_paths(self):
        """
        Return the effective number of paths in the tree.
        """
        # NB: the algorithm assume a symmetric logic tree for the GSIMs;
        # in the future we may relax such assumption
        num_branches = self.get_num_branches()
        if not sum(num_branches.values()):
            return 0
        num = 1
        for val in num_branches.values():
            if val:  # the branch is effective
                num *= val
        return num

    def _build_trts_branches(self, tectonic_region_types):
        # do the parsing, called at instantiation time to populate .values
        trts = []
        branches = []
        branchsetids = set()
        basedir = os.path.dirname(self.filename)
        for blnode in self._ltnode:
            [branchset] = _bsnodes(self.filename, blnode)
            if branchset['uncertaintyType'] != 'gmpeModel':
                raise InvalidLogicTree(
                    '%s: only uncertainties of type "gmpeModel" '
                    'are allowed in gmpe logic tree' % self.filename)
            bsid = branchset['branchSetID']
            if bsid in branchsetids:
                raise InvalidLogicTree(
                    '%s: Duplicated branchSetID %s' %
                    (self.filename, bsid))
            else:
                branchsetids.add(bsid)
            trt = branchset.get('applyToTectonicRegionType')
            if trt:  # missing in logictree_test.py
                self.bs_id_by_trt[trt] = bsid
                trts.append(trt)
            self.bs_id_by_trt[trt] = bsid
            # NB: '*' is used in scenario calculations to disable filtering
            effective = (tectonic_region_types == ['*'] or
                         trt in tectonic_region_types)
            weights = []
            branch_ids = []
            for branch in branchset:
                weight = ImtWeight(branch, self.filename)
                weights.append(weight)
                branch_id = branch['branchID']
                branch_ids.append(branch_id)
                try:
                    gsim = valid.gsim(branch.uncertaintyModel, basedir)
                except Exception as exc:
                    raise ValueError(
                        "%s in file %s" % (exc, self.filename)) from exc
                if gsim in self.values[trt]:
                    raise InvalidLogicTree('%s: duplicated gsim %s' %
                                           (self.filename, gsim))
                if len(weight.dic) > 1:
                    gsim.weight = weight
                self.values[trt].append(gsim)
                bt = BranchTuple(
                    branchset['applyToTectonicRegionType'],
                    branch_id, gsim, weight, effective)
                if effective:
                    branches.append(bt)
            tot = sum(weights)
            assert tot.is_one(), '%s in branch %s' % (tot, branch_id)
            if duplicated(branch_ids):
                raise InvalidLogicTree(
                    'There where duplicated branchIDs in %s' %
                    self.filename)
        if len(trts) > len(set(trts)):
            raise InvalidLogicTree(
                '%s: Found duplicated applyToTectonicRegionType=%s' %
                (self.filename, trts))
        branches.sort(key=lambda b: (b.trt, b.id))
        # TODO: add an .idx to each GSIM ?
        return branches

    def get_gsims(self, trt):
        """
        :param trt: tectonic region type
        :returns: sorted list of available GSIMs for that trt
        """
        if trt == '*' or trt == b'*':  # fake logictree
            [trt] = self.values
        return sorted(self.values[trt])

    def sample(self, n, seed):
        """
        :param n: number of samples
        :param seed: random seed
        :returns: n Realization objects
        """
        brlists = [sample([b for b in self.branches if b.trt == trt],
                          n, seed + i) for i, trt in enumerate(self.values)]
        rlzs = []
        for i in range(n):
            weight = 1
            lt_path = []
            lt_uid = []
            value = []
            for brlist in brlists:  # there is branch list for each TRT
                branch = brlist[i]
                lt_path.append(branch.id)
                lt_uid.append(branch.id if branch.effective else '@')
                weight *= branch.weight
                value.append(branch.gsim)
            rlz = Realization(tuple(value), weight, i, tuple(lt_uid), 1)
            rlzs.append(rlz)
        return rlzs

    def __iter__(self):
        """
        Yield :class:`openquake.commonlib.logictree.Realization` instances
        """
        groups = []
        # NB: branches are already sorted
        for trt in self.values:
            groups.append([b for b in self.branches if b.trt == trt])
        # with T tectonic region types there are T groups and T branches
        for i, branches in enumerate(itertools.product(*groups)):
            weight = 1
            lt_path = []
            lt_uid = []
            value = []
            for trt, branch in zip(self.values, branches):
                lt_path.append(branch.id)
                lt_uid.append(branch.id if branch.effective else '@')
                weight *= branch.weight
                value.append(branch.gsim)
            yield Realization(tuple(value), weight, i, tuple(lt_uid), 1)

    def __repr__(self):
        lines = ['%s,%s,%s,w=%s' %
                 (b.trt, b.id, b.gsim, b.weight['weight'])
                 for b in self.branches if b.effective]
        return '<%s\n%s>' % (self.__class__.__name__, '\n'.join(lines))


def taxonomy_mapping(filename, taxonomies):
    """
    :param filename: path to the CSV file containing the taxonomy associations
    :param taxonomies: an array taxonomy string -> taxonomy index
    :returns: (array, [[(taxonomy, weight), ...], ...])
    """
    if filename is None:  # trivial mapping
        return (), [[(taxo, 1)] for taxo in taxonomies]
    dic = {}  # taxonomy index -> risk taxonomy
    array = hdf5.read_csv(filename, {None: hdf5.vstr, 'weight': float}).array
    arr = add_defaults(array, weight=1.)
    assert arr.dtype.names == ('taxonomy', 'conversion', 'weight')
    dic = group_array(arr, 'taxonomy')
    taxonomies = taxonomies[1:]  # strip '?'
    missing = set(taxonomies) - set(dic)
    if missing:
        raise InvalidFile('The taxonomies %s are in the exposure but not in %s'
                          % (missing, filename))
    lst = [[("?", 1)]]
    for idx, taxo in enumerate(taxonomies, 1):
        recs = dic[taxo]
        if abs(recs['weight'].sum() - 1.) > pmf.PRECISION:
            raise InvalidFile('%s: the weights do not sum up to 1 for %s' %
                              (filename, taxo))
        lst.append([(rec['conversion'], rec['weight']) for rec in recs])
    return arr, lst
