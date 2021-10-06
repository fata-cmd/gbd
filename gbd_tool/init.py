# GBD Benchmark Database (GBD)
# Copyright (C) 2021 Markus Iser, Karlsruhe Institute of Technology (KIT)
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import multiprocessing
from multiprocessing import Pool

import os
from os.path import isfile

import hashlib
import csv

from gbd_tool.gbd_api import GBD, GBDException
from gbd_tool.gbd_hash import gbd_hash
from gbd_tool.util import eprint, confirm, open_cnf_file

# import faulthandler
# faulthandler.enable()

try:
    from gbdc import extract_base_features
except ImportError:
    def extract_base_features(path) -> dict:
        raise GBDException("Method 'extract_base_features' not available")

try:
    from gbdc import extract_gate_features
except ImportError:
    def extract_gate_features(path) -> dict:
        raise GBDException("Method 'extract_gate_features' not available")


# Import data from CSV file
def import_csv(api: GBD, path, key, source, target):
    if not api.feature_exists(target):
        raise GBDException("Target feature '{}' does not exist. Import canceled.".format(target))
    with open(path, newline='') as csvfile:
        csvreader = csv.DictReader(csvfile, delimiter=api.separator, quotechar='\'')
        lst = [(row[key].strip(), row[source].strip()) for row in csvreader if row[source] and row[source].strip()]
        eprint("Inserting {} values into target '{}'".format(len(lst), target))
        api.database.bulk_insert(target, lst)


# Initialize table 'local' with instances found under given path
def init_local(api: GBD, path):
    eprint('Initializing local path entries {} using {} cores'.format(path, api.jobs))
    if api.jobs == 1 and multiprocessing.cpu_count() > 1:
        eprint("Activate parallel initialization using --jobs={}".format(multiprocessing.cpu_count()))
    remove_stale_benchmarks(api)
    register_benchmarks(api, path)

def remove_stale_benchmarks(api: GBD):
    eprint("Sanitizing local path entries ... ")
    paths = api.database.value_query("SELECT value FROM local")
    sanitize = list(filter(lambda path: not isfile(path), paths))
    if len(sanitize) and confirm("{} files not found. Remove stale entries from local table?".format(len(sanitize))):
        for path in sanitize:
            api.database.submit("DELETE FROM local WHERE value='{}'".format(path))

def compute_hash(path):
    eprint('Hashing {}'.format(path))
    hashvalue = gbd_hash(path)
    attributes = [ ('INSERT', 'local', path) ]
    return { 'hashvalue': hashvalue, 'attributes': attributes }

def register_benchmarks(api: GBD, root):
    pool = Pool(min(multiprocessing.cpu_count(), api.jobs))
    for root, dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = os.path.join(root, filename)
            if any(path.endswith(suffix) for suffix in [".cnf", ".cnf.gz", ".cnf.lzma", ".cnf.xz", ".cnf.bz2"]):
                hashes = api.database.value_query("SELECT hash FROM local WHERE value = '{}'".format(path))
                if len(hashes) != 0:
                    eprint('Problem {} already hashed'.format(path))
                else:
                    handler = pool.apply_async(compute_hash, args=(path,), callback=api.callback_set_attributes_locked)
                    #handler.get()
    pool.close()
    pool.join() 


# Generic Parallel Runner
def run(api: GBD, resultset, func):
    if api.jobs == 1:
        for result in resultset:
            hashvalue = result[0].split(',')[0]
            filename = result[1].split(',')[0]
            api.callback_set_attributes_locked(func(hashvalue, filename))
    else:
        pool = Pool(min(multiprocessing.cpu_count(), api.jobs))
        for result in resultset:
            hashvalue = result[0].split(',')[0]
            filename = result[1].split(',')[0]
            pool.apply_async(func, args=(hashvalue, filename), callback=api.callback_set_attributes_locked)
        pool.close()
        pool.join()


# Initialize base feature tables for given instances
def init_base_features(api: GBD, query, hashes):
    resultset = api.query_search(query, hashes, ["local"])
    run(api, resultset, base_features)

def base_features(hashvalue, filename):
    eprint('Extracting base features from {}'.format(filename))
    rec = extract_base_features(filename)
    attributes = [ ('REPLACE', key, value) for key, value in rec.items() ]
    return { 'hashvalue': hashvalue, 'attributes': attributes }


# Initialize gate feature tables for given instances
def init_gate_features(api: GBD, query, hashes):
    resultset = api.query_search(query, hashes, ["local"])
    run(api, resultset, gate_features)

def gate_features(hashvalue, filename):
    eprint('Extracting gate features from {}'.format(filename))
    rec = extract_gate_features(filename)
    attributes = [ ('REPLACE', key, int(value) if value.is_integer() else value) for key, value in rec.items() ]
    return { 'hashvalue': hashvalue, 'attributes': attributes }


# Initialize degree_sequence_hash for given instances
def init_degree_sequence_hash(api: GBD, hashes):
    if not api.feature_exists("degree_sequence_hash"):
        api.create_feature("degree_sequence_hash", "empty")
    resultset = api.query_search(None, hashes, ["local"])
    run(api, resultset, compute_degree_sequence_hash)

def compute_degree_sequence_hash(hashvalue, filename):
    eprint('Computing degree-sequence hash for {}'.format(filename))
    hash_md5 = hashlib.md5()
    degrees = dict()
    f = open_cnf_file(filename, 'rt')
    for line in f:
        line = line.strip()
        if line and line[0] not in ['p', 'c']:
            for lit in line.split()[:-1]:
                num = int(lit)
                tup = degrees.get(abs(num), (0,0))
                degrees[abs(num)] = (tup[0], tup[1]+1) if num < 0 else (tup[0]+1, tup[1])

    degree_list = list(degrees.values())
    degree_list.sort(key=lambda t: (t[0]+t[1], abs(t[0]-t[1])))
    
    for t in degree_list:
        hash_md5.update(str(t[0]+t[1]).encode('utf-8'))
        hash_md5.update(b' ')
        hash_md5.update(str(abs(t[0]-t[1])).encode('utf-8'))
        hash_md5.update(b' ')

    f.close()

    return { 'hashvalue': hashvalue, 'attributes': [ ('REPLACE', 'degree_sequence_hash', hash_md5.hexdigest()) ] }