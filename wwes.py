#!/usr/bin/python3

from eveapi.eveapi import EVEAPIConnection
import pickle, zlib
import os, errno
import tempfile
import time
from itertools import chain
from sqlalchemy import *
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import argparse

cfg = None
metadata = MetaData()
Base = declarative_base(metadata=metadata)

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
	return resolved_types.get(id) or ("Type %d" % id)

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
		yield l[i:i+250]

class Item(Base):
	__tablename__ = 'assets'
	id = Column(Integer, primary_key=True)
	tid = Column(Integer)
	quantity = Column(Integer)
	location = Column(Integer)
	flag = Column(Integer)
	name = Column(String(100))

	def __init__(self, id, tid, q, location, flag, rawQ=None):
		self.id = id
		self.tid = tid
		self.quantity = rawQ or q or 1
		self.location = location
		self.flag = flag

	def __str__(self):
		return "%s %s" % (resolved_types[self.tid], quantity(self.quantity))

	def is_container(self):
		return 'contents' in self.__dict__;

class Division(Base):
	__tablename__ = 'divisions'
	id = Column(Integer, primary_key=True)
	w_name = Column(String(100))
	h_name = Column(String(100))
	balance = Column(Float)

	def __init__(self, id):
		self.id = id

class Location(object):
	item = None
	def __init__(self, lid):
		self.lid = lid
		self.sublocations = {}

	def set_item(self, item):
		self.item = item

	def append(self, item, sublocation):
		if sublocation not in self.sublocations:
			self.sublocations[sublocation] = []
		self.sublocations[sublocation].append(item)

	def flags(self):
		return self.sublocations.keys()

	def get_by_flag(self, sublocation):
		return self.sublocations[sublocation]

	def is_object(self):
		return self.item is not None

	def __str__(self):
		if self.item is None:
			return "Station %s" % resolved_locations[self.lid]
		if self.item.tid == 27:
			return "Office %s slot %d" % (resolved_locations[self.item.location], self.item.flag - 69)
		return "%s '%s'" % (resolved_types[self.item.tid], self.item.name)

my_format = "{0:<30}:{1:>20,.2f}".format
class CorpState(Base):
	__tablename__ = 'overview'
	id = Column(Integer, primary_key=True)
	name = Column(String(100))
	ticker = Column(String(5))
	shares = Column(Integer)
	ceo = Column(Integer)
	alliance = Column(Integer)
	tax = Column(Float)
	members = Column(Integer)
	date = Column(Integer)

	def __init__(self, debug=False):
		self.debug = debug

	def fetch(self, keyID, vCode):
		self.auth = api.auth(keyID=keyID, vCode=vCode)
		key = self.auth.account.ApiKeyInfo().key
		if key.type != 'Corporation' or key.expires != "" and time.time() < key.expires:
			raise ValueError

		self.date = time.time()

		info = self.auth.corp.CorporationSheet()
		self.divisions = {}
		for wallet in info.walletDivisions:
			division = Division(wallet.accountKey)
			division.w_name = wallet.description
			self.divisions[wallet.accountKey] = division
		for hangar in info.divisions:
			self.divisions[hangar.accountKey].h_name = hangar.description

		self.id = info.corporationID
		self.ticker = info.ticker
		self.dumps_dir = os.path.join(cfg.dumps, self.ticker) 

		self.name = info.corporationName
		self.members = info.memberCount
		self.ceo = info.ceoID
		self.alliance = info.allianceID
		self.tax = info.taxRate

		self.shares = 0
		self.shareholders = []
		holders = self.auth.corp.ShareHolders()
		for sh in chain(holders.characters, holders.corporations):
			self.shareholders.append((sh.shareholderName, sh.shares))
			self.shares += sh.shares

		self.balance = 0.0
		for account in self.auth.corp.AccountBalance().accounts:
			b = float(account.balance)
			self.divisions[account.accountKey].balance = b
			self.balance += b

		self.assets = {}
		for c in self.auth.corp.AssetList(flat=1).assets:
#			if self.debug:
#				print("Got %s x%d %d in %s" % (type_name(c.typeID), c.quantity or 1, c.itemID, self.location(c)))
			if 'rawQuantity' in c:
				q = c.rawQuantity
			elif 'quantity' in c:
				q = c.quantity
			else:
				q = 1
			self.assets[c.itemID] = Item(c.itemID, c.typeID, q, c.locationID, c.flag)

		self.process_assets()

	def process_assets(self):
		self.offices = []
		self.locations = {}
		types = {}
		locations = {}
		for i in self.assets.values():
			if not i.tid in resolved_types:
				types[i.tid] = 1
			if i.tid == 27:
				self.offices.append(i.id)

			if not i.location in self.locations:
				self.locations[i.location] = Location(i.location)

			self.locations[i.location].append(self.assets[i.id], i.flag)

			# is this object a container?
			if i.id in self.locations:
				self.locations[i.id].set_item(self.assets[i.id])
				self.assets[i.id].contents = self.locations[i.id]

			# have we seen the container before?
			if i.location in self.assets:
				self.locations[i.location].set_item(self.assets[i.location])
				self.assets[i.location].contents = self.locations[i.location]

		# resolve type names
		for type_chunk in chunks(types.keys()):
			for t in api.eve.TypeName(ids=type_chunk).types:
				resolved_types[t.typeID] = t.typeName

		containers_to_resolve = []
		locations_to_resolve = {}
		for i, c in self.locations.items():
			c.name = resolved_locations.get(i) or resolved_containers.get(i)
			if c.name:
				continue
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
			for c in self.auth.corp.Locations(ids=c_chunk).locations:
				resolved_containers[c.itemID] = c.itemName


		for i, v in self.assets.items():
			if i in self.locations:
				if v.tid == 27:
					v.name = "Slot %d" % (v.flag - 69)
				else:
					v.name = resolved_containers[i]

	def save(self):
		if not os.path.exists(self.dumps_dir):
			os.makedirs(self.dumps_dir)
		self.db = create_engine('sqlite:///%s/%d.db' % (self.dumps_dir, self.date), echo=self.debug)
		metadata.create_all(bind=self.db)
		s = sessionmaker(bind=self.db)()
		s.add_all(list(self.assets.values()))
		s.add_all(list(self.divisions.values()))
		s.add(self)
		s.commit()

	def load_prev(self):
		names = os.listdir(current_state.dumps_dir)
		if not names:
			return None

		# max(names) does alphabetical sort
		# since each 'name' is <unix timestamp>.db this is ok for now
		# it will break on Sat, 20 Nov 2286 17:46:40 GMT though
		# not sure if python3 will still be available by that time
		db = create_engine('sqlite:///%s/%s' % (self.dumps_dir, max(names)), echo=self.debug)

		s = sessionmaker(bind=db)()
		loaded = s.query(CorpState).first()
		loaded.db = db
		loaded.debug = self.debug

		loaded.load_from_db()
		return loaded

	def load_from_db(self):
		s = sessionmaker(bind=self.db)()
		self.divisions = {d.id: d for d in s.query(Division).all()}
		self.balance = sum([d.balance for d in self.divisions.values()])
		self.assets = {i.id: i for i in s.query(Item).all()}
		self.process_assets()

	# should be rewritten to properly recurse through assets
	def print(self):
		print("Corporation \"%s\"" % self.name)
		print()
		print("Wallets:")
		for i, w in self.divisions.items():
			print("\t", my_format(w.w_name, w.balance))
		print("\t", my_format("Total", self.balance))
		print()
		print(my_format("Shares", self.shares))
		print(my_format("Per share", self.balance/self.shares))
		print()
		print("Offices:")
		for oid in self.offices:
			office = self.locations[oid]
			print("\t", office)
			for s in office.flags():
				if s == 4:
					hangar = self.divisions[1000]
				else:
					hangar = self.divisions[1000 + s - 115]
				print("\t\t", hangar.h_name)
				for i in office.get_by_flag(s):
					if i.is_container():
						print("\t\t\t", i.contents)
						for f in i.contents.flags():
							if f == 63:
								print("\t\t\tLocked")
							else:
								print("\t\t\tUnlocked")
							for ii in i.contents.get_by_flag(f):
								print("\t\t\t\t", ii)
					else:
						print("\t\t\t", i)
		print()

class MyCacheHandler(object):
	def __init__(self, debug=False):
		self.debug = debug
		self.count = 0
		self.cache = {}
		self.tempdir = os.path.join(tempfile.gettempdir(), "eveapi")
		if not os.path.exists(self.tempdir):
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
			cacheFile = os.path.join(self.tempdir, str(key) + ".cache")
			if os.path.exists(cacheFile):
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
			cacheFile = os.path.join(self.tempdir, str(key) + ".cache")
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

		for p in ['dumps', 'reports']:
			self.data[p] = os.path.expanduser(self.data[p][0])
			try:
				os.makedirs(self.data[p])
			except OSError as exc:
				if exc.errno == errno.EEXIST:
					pass
				else:
					raise

	def __getattr__(self, this):
		return self.data[this]

	def __hasattr__(self, this):
		return this in self.data

if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--debug", help="Show lots of debug output", action="store_true")
	parser.add_argument("--no-save", help="Do not save the current state", action="store_true")
	parser.add_argument("--quiet", help="Do not print corporation data", action="store_true")
	args = parser.parse_args()

	api = EVEAPIConnection(cacheHandler=MyCacheHandler(debug=args.debug))
	current_state = CorpState(debug=args.debug)
	cfg = WWESConfig()

	for (keyID, vCode) in cfg.keys:
		try:
			current_state.fetch(keyID, vCode)
		except Exception as e:
			if args.debug:
				print(str(e))
				raise
			continue
		else:
			break
	else:
		print("No working key pair found")
		exit(-1)

	# free the chain and opened files and other stuff
	cfg.keys = None

	if not args.quiet:
		current_state.print()

	prev_state = current_state.load_prev()

	if not args.no_save:
		current_state.save()

	if not prev_state:
		print("No previous dumps found")
		exit(0)

	# compare stuff
	# also fetch transactions in between
	# store everything as report

