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

resolved_containers = {
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

def chunks(l):
	l = list(l)
	for i in range(0, len(l), 250):
		yield ",".join(map(str, l[i:i+250]))

class Item(object):
	def __init__(self, tid, q, location, rawQ=None):
		self.tid = tid
		self.q = rawQ or q or 1
		self.location = location
	def __str__(self):
		return "%s %s" % (resolved_types[self.tid], quantity(self.q))

class Location(object):
	item = None
	sublocations = {}
	def __init__(self, lid):
		self.lid = lid

	def set_item(self, item):
		self.item = item

	def append(self, item, sublocation):
		if not sublocation in self.sublocations:
			self.sublocations[sublocation] = []
		self.sublocations[sublocation].append(item)

	def flags(self):
		return self.sublocations.keys()

	def get_by_flag(self, sublocation):
		return self.sublocations[sublocation]

	def is_object(self):
		return not self.item is None

	def __str__(self):
		if self.item is None:
			return "Station %s" % resolved_locations[self.lid]
		if self.item.tid == 27:
			return "Office %s slot %d" % (resolved_locations[self.item.location[0]], self.item.location[1] - 69)
		return "%s '%s'" % (resolved_types[self.item.tid], resolved_containers[self.lid])

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

		assets = {}
		types = {}
		locations = {}
		self.locations = {}
		for c in auth.corp.AssetList(flat=1).assets:
#			if self.debug:
#				print("Got %s x%d %d in %s" % (type_name(c.typeID), c.quantity or 1, c.itemID, self.location(c)))
			if 'rawQuantity' in c:
				q = c.rawQuantity
			elif 'quantity' in c:
				q = c.quantity
			else:
				q = 1
			item = Item(c.typeID, q, (c.locationID, c.flag))

			assets[c.itemID] = item
			if not c.typeID in resolved_types:
				types[c.typeID] = 1

			if not c.locationID in self.locations:
				self.locations[c.locationID] = Location(c.locationID)

			self.locations[c.locationID].append(item, c.flag)

			# is this object a container?
			if c.itemID in self.locations:
				self.locations[c.itemID].set_item(item)

			# have we seen the container before?
			if c.locationID in assets:
				self.locations[c.locationID].set_item(assets[c.locationID])

		# resolve type names
		for type_chunk in chunks(types.keys()):
			for t in api.eve.TypeName(ids=type_chunk).types:
				resolved_types[t.typeID] = t.typeName

		containers_to_resolve = []
		locations_to_resolve = {}
		for i, c in self.locations.items():
			if c.is_object():
				if c.item.tid != 27: # 27 is Office
					containers_to_resolve.append(i)
			else:
				# these have to be modified first
				if i >= 66000000 and i < 67000000:
					locations_to_resolve[i - 6000001] = i
				elif i >= 67000000 and i < 68000000:
					locations_to_resolve[i - 6000000] = i
				else:
					locations_to_resolve[i] = i

		# resolve location names
		# using CharacterName to resolve station names!
		for l_chunk in chunks(locations_to_resolve.keys()):
			for l in api.eve.CharacterName(IDs=l_chunk).characters:
				resolved_locations[locations_to_resolve[l.characterID]] = l.name

		# resolve container names
		for c_chunk in chunks(containers_to_resolve):
			for c in auth.corp.Locations(ids=c_chunk).locations:
				resolved_containers[c.itemID] = c.itemName

		for l in self.locations.values():
			print(l)

	def print(self):
		print("Corporation \"%s\"" % self.name)
		print()
		print("Wallets:")
		for i, w in self.wallets.items():
			print("\t", my_format(w['name'], w['balance']))
		print("\t", my_format("Total", self.balance))
		print()
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
	api = EVEAPIConnection(cacheHandler=MyCacheHandler(debug=debug))
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

