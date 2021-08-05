import asyncio
import logging
import json

from . import tools
from . import glob

class Manager:
    lock = asyncio.Lock()

    def __init__(self, host, port, key):
        self.host = host
        self.port = port
        self.key = key
        self.sp_pubkey = None

        self.config = tools.create_tmp(suffix=".json")
        with open(self.config, "w") as f:
            json.dump(self.dump(), f)


    @staticmethod
    def load(man_dict, config):
        host = man_dict['host']
        port = man_dict['port']
        key = man_dict['key']

        return Manager(host, port, key)


    def dump(self):
        return {
            "host": self.host,
            "port": self.port,
            "key": self.key
        }

    async def get_sp_pubkey(self):
        async with self.lock:
            if self.sp_pubkey is not None:
                return self.sp_pubkey

            args = "--config {} --request get-pub-key --data aa".format(self.config).split()
            out, _ = await tools.run_async_output(glob.ATTMAN_CLI, *args)

            self.sp_pubkey = tools.create_tmp(suffix=".pem")
            with open(self.sp_pubkey, "wb") as f:
                f.write(out)

            return self.sp_pubkey