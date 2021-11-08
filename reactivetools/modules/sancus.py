import logging
import asyncio
import binascii
from enum import Enum
from collections import namedtuple
import json
import yaml
import os

from elftools.elf import elffile

from .base import Module
from ..nodes import SancusNode
from .. import tools
from .. import glob
from ..crypto import Encryption
from ..dumpers import *
from ..loaders import *


class Error(Exception):
    pass


class SancusModule(Module):
    def __init__(self, name, node, priority, deployed, nonce, attested, files,
                 cflags, ldflags, binary, id, symtab, key):
        self.out_dir = os.path.join(glob.BUILD_DIR, "sancus-{}".format(name))
        super().__init__(name, node, priority, deployed, nonce, attested, self.out_dir)

        self.files = files
        self.cflags = cflags
        self.ldflags = ldflags

        self.__build_fut = tools.init_future(binary)
        self.__deploy_fut = tools.init_future(id, symtab)
        self.__key_fut = tools.init_future(key)
        self.__attest_fut = tools.init_future(attested if attested else None)

    @staticmethod
    def load(mod_dict, node_obj):
        name = mod_dict['name']
        node = node_obj
        priority = mod_dict.get('priority')
        deployed = mod_dict.get('deployed')
        nonce = mod_dict.get('nonce')
        attested = mod_dict.get('attested')
        files = load_list(mod_dict['files'],
                          lambda f: parse_file_name(f))
        cflags = load_list(mod_dict.get('cflags'))
        ldflags = load_list(mod_dict.get('ldflags'))
        binary = parse_file_name(mod_dict.get('binary'))
        id = mod_dict.get('id')
        symtab = parse_file_name(mod_dict.get('symtab'))
        key = parse_key(mod_dict.get('key'))

        return SancusModule(name, node, priority, deployed, nonce, attested,
                            files, cflags, ldflags, binary, id, symtab, key)

    def dump(self):
        return {
            "type": "sancus",
            "name": self.name,
            "node": self.node.name,
            "priority": self.priority,
            "deployed": self.deployed,
            "nonce": self.nonce,
            "attested": self.attested,
            "files": dump(self.files),
            "cflags": dump(self.cflags),
            "ldflags": dump(self.ldflags),
            "binary": dump(self.binary) if self.deployed else None,
            "id": dump(self.id) if self.deployed else None,
            "symtab": dump(self.symtab) if self.deployed else None,
            "key": dump(self.key) if self.deployed else None
        }

    # --- Properties --- #

    @property
    async def binary(self):
        return await self.build()

    @property
    async def id(self):
        id, _ = await self.deploy()
        return id

    @property
    async def symtab(self):
        _, symtab = await self.deploy()
        return symtab

    @property
    async def key(self):
        if self.__key_fut is None:
            self.__key_fut = asyncio.ensure_future(self._calculate_key())

        return await self.__key_fut

    # --- Implement abstract methods --- #

    async def build(self):
        if self.__build_fut is None:
            self.__build_fut = asyncio.ensure_future(self.__build())

        return await self.__build_fut

    async def deploy(self):
        if self.__deploy_fut is None:
            self.__deploy_fut = asyncio.ensure_future(self.node.deploy(self))

        return await self.__deploy_fut

    async def attest(self):
        if glob.get_att_man():
            await self.__attest_manager()
        else:
            if self.__attest_fut is None:
                self.__attest_fut = asyncio.ensure_future(
                    self.node.attest(self))

            await self.__attest_fut

    async def get_id(self):
        return await self.id

    async def get_input_id(self, input):
        return await self.get_io_id(input)

    async def get_output_id(self, output):
        return await self.get_io_id(output)

    async def get_entry_id(self, entry):
        # If it is a number, that is the ID (given by the deployer)
        if entry.isnumeric():
            return int(entry)

        return await self._get_entry_id(entry)

    async def get_key(self):
        return await self.key

    @staticmethod
    def get_supported_nodes():
        return [SancusNode]

    @staticmethod
    def get_supported_encryption():
        return [Encryption.SPONGENT]

    # --- Static methods --- #

    @staticmethod
    def _get_build_config(verbosity):
        if verbosity == tools.Verbosity.Debug:
            flags = ['--debug']
        # elif verbosity == tools.Verbosity.Verbose:
        #     flags = ['--verbose']
        else:
            flags = []

        cflags = flags
        ldflags = flags + ['--inline-arithmetic']

        return _BuildConfig(cc='sancus-cc', cflags=cflags,
                            ld='sancus-ld', ldflags=ldflags)

    # --- Others --- #

    async def get_io_id(self, io):
        # If io is a number, that is the ID (given by the deployer)
        if isinstance(io, int):
            return io

        return await self._get_io_id(io)

    async def __build(self):
        logging.info('Building module %s from %s',
                     self.name, ', '.join(map(str, self.files)))

        config = self._get_build_config(tools.get_verbosity())
        objects = {str(p): tools.create_tmp(
            suffix='.o', dir=self.out_dir) for p in self.files}

        cflags = config.cflags + self.cflags
        def build_obj(c, o): return tools.run_async(config.cc, *cflags,
                                                    '-c', '-o', o, c)
        build_futs = [build_obj(c, o) for c, o in objects.items()]
        await asyncio.gather(*build_futs)

        binary = tools.create_tmp(suffix='.elf', dir=self.out_dir)
        ldflags = config.ldflags + self.ldflags

        # prepare the config file for this SM
        ldflags = await self.__prepare_config_file(ldflags)

        await tools.run_async(config.ld, *ldflags,
                              '-o', binary, *objects.values())
        return binary

    async def __prepare_config_file(self, ldflags):
        # try to get sm config file if present in self.ldflags
        # otherwise, create empty file
        try:
            # fetch the name
            flag = next(filter(lambda x: "--sm-config-file" in x, ldflags))
            config_file = flag.split()[1]

            # open the file and parse it
            with open(config_file, "r") as f:
                config = yaml.load(f)
        except:
            # we create a new file with empty config
            config_file = tools.create_tmp(suffix='.yaml', dir=self.out_dir)
            config = {self.name: []}

            # remove old flag if present, append new one
            ldflags = list(
                filter(lambda x: "--sm-config-file" not in x, ldflags))
            ldflags.append("--sm-config-file {}".format(config_file))

        # override num_connections if the value is present and is < self.connections
        if "num_connections" not in config or config["num_connections"] < self.connections:
            config[self.name].append({"num_connections": self.connections})

        # save changes
        with open(config_file, "w") as f:
            yaml.dump(config, f)

        return ldflags

    async def _calculate_key(self):
        linked_binary = await self.__link()

        args = "{} --gen-sm-key {} --key {}".format(
            linked_binary, self.name, self.node.vendor_key
        ).split()

        key, _ = await tools.run_async_output("sancus-crypto", *args)
        logging.info('Module key for %s: %s', self.name, key)

        return binascii.unhexlify(key)

    async def __link(self):
        linked_binary = tools.create_tmp(suffix='.elf', dir=self.out_dir)

        # NOTE: we use '--noinhibit-exec' flag because the linker complains
        #       if the addresses of .bss section are not aligned to 2 bytes
        #       using this flag instead, the output file is still generated
        await tools.run_async('msp430-ld', '-T', await self.symtab,
                              '-o', linked_binary, '--noinhibit-exec', await self.binary)
        return linked_binary

    async def _get_io_id(self, io_name):
        sym_name = '__sm_{}_io_{}_idx'.format(self.name, io_name)
        symbol = await self.__get_symbol(sym_name)

        if symbol is None:
            raise Error('Module {} has no endpoint named {}'
                        .format(self.name, io_name))

        return symbol

    async def _get_entry_id(self, entry_name):
        sym_name = '__sm_{}_entry_{}_idx'.format(self.name, entry_name)
        symbol = await self.__get_symbol(sym_name)

        if symbol is None:
            raise Error('Module {} has no entry named {}'
                        .format(self.name, entry_name))

        return symbol

    async def __get_symbol(self, name):
        if not await self.binary:
            raise Error("ELF file not present for {}, cannot extract symbol ID of {}".format(
                self.name, name))

        with open(await self.binary, 'rb') as f:
            elf = elffile.ELFFile(f)
            for section in elf.iter_sections():
                if isinstance(section, elffile.SymbolTableSection):
                    for symbol in section.iter_symbols():
                        sym_section = symbol['st_shndx']
                        if symbol.name == name and sym_section != 'SHN_UNDEF':
                            return symbol['st_value']

    async def __attest_manager(self):
        data = {
            "id": await self.id,
            "name": self.name,
            "host": str(self.node.ip_address),
            "port": self.node.reactive_port,
            "em_port": self.node.reactive_port,
            "key": list(await self.key)
        }
        data_file = tools.create_tmp(suffix=".json")
        with open(data_file, "w") as f:
            json.dump(data, f)

        args = "--config {} --request attest-sancus --data {}".format(
            self.manager.config, data_file).split()
        out, _ = await tools.run_async_output(glob.ATTMAN_CLI, *args)
        key_arr = eval(out)  # from string to array
        key = bytes(key_arr)  # from array to bytes

        if await self.key != key:
            raise Error(
                "Received key is different from {} key".format(self.name))

        logging.info("Done Remote Attestation of {}. Key: {}".format(
            self.name, key_arr))
        self.attested = True


_BuildConfig = namedtuple('_BuildConfig', ['cc', 'cflags', 'ld', 'ldflags'])
