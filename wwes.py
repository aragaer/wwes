#!/usr/bin/python3

from eveapi.eveapi import EVEAPIConnection
import pickle, zlib
import os
from os.path import join, exists
import tempfile
import time
from itertools import chain

debug = True
resolved_types = {
	27:		'Office',
	654:		'Iteron Mark II',
	2203:		'Acolyte I',
	2454:		'Hobgoblin I',
	23087:		'Amarr Encryption Method',
	90877978:	'Station Container',
	1270092916:	'Station Vault',
}

resolved_locations = {
}

def type_name(id):
	if id in resolved_types:
		return resolved_types[id]
	else:
		return "Type %d" % id

def quantity(q):
	if q == -1:
		return "Original"
	elif q == -2:
		return "Copy"
	else:
		return "x%d" % q

my_format = "{0:<30}:{1:>20,.2f}".format
class CorpState(object):
	def __init__(self, debug=False):
		self.debug = debug

	def location(self, row):
		office = row.locationID
		return "%s flag %s" % (office, row.flag)

	def fetch(self, keyID, vCode):
		auth = api.auth(keyID=keyID, vCode=vCode)
		key = auth.account.ApiKeyInfo().key
		if key.type != 'Corporation' or key.expires != "" and time.time() < key.expires:
			raise ValueError

		self.date = time.time()

		self.name = key.characters[0].corporationName

		info = auth.corp.CorporationSheet()
		self.wallets = {}
		self.hangars = {}
		for wallet in info.walletDivisions:
			self.wallets[wallet.accountKey] = {'name': wallet.description}
		for hangar in info.divisions:
			self.hangars[hangar.accountKey] = hangar.description

		self.shares = 0
		self.shareholders = []
		holders = auth.corp.ShareHolders()
		for sh in chain(holders.characters, holders.corporations):
			self.shareholders.append((sh.shareholderName, sh.shares))
			self.shares += sh.shares

		self.balance = 0.0
		for account in auth.corp.AccountBalance().accounts:
			b = float(account.balance)
			self.wallets[account.accountKey]['balance'] = b
			self.balance += b

		self.assets = {}
		self.neat_assets = {}
		self.containers = {}
		self.offices = []
		self.deliveries = {}
		types = {}
		stuff_location = {}
		locations = {}
		for c in auth.corp.AssetList(flat=1).assets:
#			if self.debug:
#				print("Got %s x%d %d in %s" % (type_name(c.typeID), c.quantity or 1, c.itemID, self.location(c)))
			if 'rawQuantity' in c:
				q = c.rawQuantity
			elif 'quantity' in c:
				q = c.quantity
			else:
				q = 1
			self.assets[(c.typeID, c.locationID, c.flag)] = q
			if not c.typeID in resolved_types:
				types[c.typeID] = 1

			if not c.locationID in self.neat_assets:
				self.neat_assets[c.locationID] = {}

			if not c.flag in self.neat_assets[c.locationID]:
				self.neat_assets[c.locationID][c.flag] = {}

			self.neat_assets[c.locationID][c.flag][c.typeID] = q

			stuff_location[c.itemID] = c.locationID
			if not c.locationID in self.containers:
				if c.locationID in stuff_location:
					self.containers[c.locationID] = stuff_location[c.locationID][0]
				else:
					self.containers[c.locationID] = 0
			elif c.itemID in self.containers:
				self.containers[c.itemID] = c.locationID
			stuff_location[c.itemID] = (c.locationID, c.flag)

			if c.typeID == 27: # Office
				self.offices.append(c.itemID)
				locations[c.locationID] = 1
			if c.flag == 62:
				self.deliveries[c.locationID] = 1
				locations[c.locationID] = 1

		self.deliveries = list(self.deliveries.keys())

		types = list(types.keys())
		for type_chunk in [types[i:i+250] for i in range(0, len(types), 250)]:
			for t in api.eve.TypeName(ids=",".join(map(str, type_chunk))).types:
				resolved_types[t.typeID] = t.typeName

		for office in self.offices:
			print("Got office %s in %s (%d)" % (office, stuff_location[office][0], stuff_location[office][1]))

		locations_to_resolve = {}
		for l in locations.keys():
			if l >= 66000000 and l < 67000000:
				locations_to_resolve[l - 6000001] = l
			elif l >= 67000000 and l < 68000000:
				locations_to_resolve[l - 6000000] = l
			else:
				locations_to_resolve[l] = l
		ll = list(locations_to_resolve.keys())

		# Using CharacterName to resolve station names!
		for l_chunk in [ll[i:i+250] for i in range(0, len(ll), 250)]:
			for l in api.eve.CharacterName(IDs=",".join(map(str, l_chunk))).characters:
				resolved_locations[locations_to_resolve[l.characterID]] = l.name

		if debug and False:
			for ((tid, lid, flag), q) in self.assets.items():
				print("Got %s %s in %s" % (type_name(tid), quantity(q), lid))

		for office in self.offices:
			print("Got office %s in %s Slot %d" % (office, resolved_locations[stuff_location[office][0]], stuff_location[office][1] - 69))

	def print(self):
		print("Corporation \"%s\"" % self.name)
		for i, w in self.wallets.items():
			print(my_format(w['name'], w['balance']))
		print(my_format("Total", self.balance))
		print(my_format("Shares", self.shares))
		print(my_format("Per share", self.balance/self.shares))
		print()

class MyCacheHandler(object):
	def __init__(self, debug=False):
		self.debug = debug
		self.count = 0
		self.cache = {}
		self.tempdir = join(tempfile.gettempdir(), "eveapi")
		if not exists(self.tempdir):
			os.makedirs(self.tempdir)

	def log(self, what):
		if self.debug:
			print("[%d] %s" % (self.count, what))

	def retrieve(self, host, path, params):
		# eveapi asks if we have this request cached
		key = hash((host, path, frozenset(params.items())))

		self.count += 1  # for logging

		# see if we have the requested page cached...
		cached = self.cache.get(key, None)
		if cached:
			cacheFile = None
		else:
			# it wasn't cached in memory, but it might be on disk.
			cacheFile = join(self.tempdir, str(key) + ".cache")
			if exists(cacheFile):
				self.log("%s: retrieving from disk" % path)
				f = open(cacheFile, "rb")
				cached = self.cache[key] = pickle.loads(zlib.decompress(f.read()))
				f.close()

		if cached:
			# check if the cached doc is fresh enough
			if time.time() < cached[0]:
				self.log("%s: returning cached document" % path)
				return cached[1]  # return the cached XML doc

			# it's stale. purge it.
			self.log("%s: cache expired, purging!" % path)
			del self.cache[key]
			if cacheFile:
				os.remove(cacheFile)

		self.log("%s: not cached, fetching from server..." % path)
		# we didn't get a cache hit so return None to indicate that the data
		# should be requested from the server.
		return None

	def store(self, host, path, params, doc, obj):
		# eveapi is asking us to cache an item
		key = hash((host, path, frozenset(params.items())))

		cachedFor = obj.cachedUntil - obj.currentTime
		if cachedFor:
			self.log("%s: cached (%d seconds)" % (path, cachedFor))

			cachedUntil = time.time() + cachedFor

			# store in memory
			cached = self.cache[key] = (cachedUntil, doc)

			# store in cache folder
			cacheFile = join(self.tempdir, str(key) + ".cache")
			with open(cacheFile, "wb") as f:
				f.write(zlib.compress(pickle.dumps(cached, -1)))

def readfile(filename, separator="="):
	with open(filename) as f:
		for line in map(str.strip, f.readlines()):
			if not line.startswith('#'):
				yield map(str.strip, line.split(separator))

class WWESConfig(object):
	data = {}
	def __init__(self, path = 'config'):
		for (key, value) in readfile(path):
			if key in self.data:
				self.data[key].append(value)
			else:
				self.data[key] = [value]

		self.data['keys'] = chain.from_iterable([readfile(f, ':') for f in self.data['keys']])

	def __getattr__(self, this):
		return self.data[this]

	def __hasattr__(self, this):
		return this in self.data

if __name__ == "__main__":
	api = EVEAPIConnection(cacheHandler=MyCacheHandler(debug=True))
	current_state = CorpState(debug=debug)
	cfg = WWESConfig()

	for (keyID, vCode) in cfg.keys:
		try:
			current_state.fetch(keyID, vCode)
		except Exception as e:
			if debug:
				print(str(e))
				raise
			continue
		else:
			break
	else:
		print("No working key pair found")
		exit(-1)

	current_state.print()

