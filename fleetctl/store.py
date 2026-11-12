"""Collector results and history. Collectors write here from background threads; build() reads it."""
import json
import os
import threading
import time


class Store:
    def __init__(self, data_dir):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.hosts = {}        # host name -> last probe result
        self.scans = {}        # host name -> last agent scan
        self.accounts = []     # normalized accounts from all sources
        self.source_issues = []
        self.tailscale = {}    # lowercased host name -> {"online": bool}
        self.errors = {}       # collector key -> {"msg", "at"}
        self.load_hist = {}    # host name -> [[t, load1, cpu, mem_pct, swap_in_mbps, psi_some], ...]
        self.acct_hist = []    # [[t, provider, account id, weekly % used, weekly reset], ...]
        self.updated = {}
        self.started = time.time()
        self._read_history()

    def _path(self):
        return os.path.join(self.data_dir, "account-history.json")

    def _read_history(self):
        try:
            with open(self._path()) as f:
                self.acct_hist = json.load(f)
        except (OSError, ValueError):
            self.acct_hist = []

    def save_history(self):
        tmp = self._path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.acct_hist, f)
        os.replace(tmp, self._path())

    def set_error(self, key, err):
        with self.lock:
            if err:
                self.errors[key] = {"msg": str(err)[:300], "at": time.time()}
            else:
                self.errors.pop(key, None)
