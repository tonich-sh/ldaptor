from ldaptor.protocols.ldap import ldapclient, distinguishedname, ldapconnector, ldaperrors, ldapsyntax
from ldaptor.protocols import pureldap
from ldaptor import ldapfilter

from twisted.internet import defer, reactor
from twisted.internet import main, app, protocol
from twisted.python.failure import Failure
from twisted.cred.identity import Identity
from twisted.cred.perspective import Perspective
from twisted.cred.authorizer import Authorizer
from twisted.cred.service import Service

class LDAPAuth(ldapclient.LDAPClient):
    def connectionMade(self):
	ldapclient.LDAPClient.connectionMade(self)
	d=self.bind(self.factory.dn, self.factory.auth)
	del self.factory.auth
	d.addErrback(self._unbind)
	d.chainDeferred(self.factory.defe)

    def _unbind(self, fail):
	self.unbind()
	raise fail

class LDAPAuthFactory(protocol.ClientFactory):
    protocol = LDAPAuth

    def __init__(self, defe, dn, auth, cb_connectionLost):
	self.defe = defe
	self.dn = dn
	self.auth = auth
	self.cb_connectionLost = cb_connectionLost
	self.proto=None

    def __getstate__(self):
        r={}
        r.update(self.__dict__)
        r['proto']=None
        return r

    def clientConnectionFailed(self, connector, reason):
	self.proto=None
	self.defe.errback("connection failed")

    def clientConnectionLost(self, connector, reason):
	self.proto=None
	self.cb_connectionLost(reason)

    def buildProtocol(self, addr):
	p=protocol.ClientFactory.buildProtocol(self, addr)
	self.proto=p
	return p

class LDAPIdentity(Identity):
    def __init__(self, name, dn, authorizer,
		 ldapClientFactoryFactory,
		 serviceLocationOverride=None):
	Identity.__init__(self, name, authorizer)
	self.dn=dn
	self.ldapClientFactoryFactory=ldapClientFactoryFactory
	self.ldapClientFactory=None
	self.serviceLocationOverride=serviceLocationOverride
	self.notify_on_connectionLost = []

    def setPassword(self, plaintext):
	raise NotImplementedError

    def setAlreadyHashedPassword(self, cyphertext):
	raise NotImplementedError

    def challenge(self):
	raise NotImplementedError

    def verifyPassword(self, challenge, hashedPassword):
	raise NotImplementedError

    def getLDAPClient(self):
	if self.ldapClientFactory is None:
	    return None
	return self.ldapClientFactory.proto

    def verifyPlainPassword(self, plaintext):
	assert not self.getLDAPClient()
	if not plaintext:
	    return defer.fail(Failure('You must enter a password.'))

	pwrq = defer.Deferred()
	self.ldapClientFactory = self.ldapClientFactoryFactory(
	    pwrq, self.dn, plaintext, self.connectionLost)

	dn = distinguishedname.DistinguishedName(stringValue=self.dn)
	c = ldapconnector.LDAPConnector(reactor, dn, self.ldapClientFactory,
					overrides=self.serviceLocationOverride)
	c.connect()
	return pwrq

    def connectionLost(self, reason):
	self.ldapClientFactory=None
	for cb in self.notify_on_connectionLost:
	    cb()
	self.notify_on_connectionLost = []

    def notifyOnConnectionLost(self, cb):
	self.notify_on_connectionLost.append(cb)

    def cancelNotifyOnConnectionLost(self, cb):
	self.notify_on_connectionLost.remove(cb)

class LDAPPerspective(Perspective):
    def __init__(self, perspectiveName, identityName="Nobody"):
	Perspective.__init__(self, perspectiveName, identityName=identityName)
	self.sessions={}

    def attached(self, reference, identity):
	try:
	    self.sessions[reference]+=1
	except KeyError:
	    self.sessions[reference]=1
	identity.notifyOnConnectionLost(self.connectionLost)
	return Perspective.attached(self, reference, identity)

    def detached(self, reference, identity):
	identity.cancelNotifyOnConnectionLost(self.connectionLost)
	self.sessions[reference]-=1
	if self.sessions[reference]==0:
	    del self.sessions[reference]
	return Perspective.detached(self, reference, identity)

    def connectionLost(self):
	for session in self.sessions.keys():
	    session.expire()

class LDAPSearchIdentity(ldapclient.LDAPSearch):
    def __init__(self, deferred, client,
		 baseObject, filter):
	ldapclient.LDAPSearch.__init__(self, deferred, client,
				       baseObject=baseObject,
				       filter=filter,
				       typesOnly=1,
				       sizeLimit=1)
	self.found = None

	deferred.addCallbacks(callback=self.process,
			      errback=lambda x: x)

    def process(self, dummy):
	if self.found:
	    return self.found
	else:
	    raise ldaperrors.LDAPUnknownError(ldaperrors.other, "unauthorized")

    def handle_entry(self, objectName, attributes):
	if not self.found:
	    self.found = str(objectName)
	else:
	    raise "GOT DUPLICATES (TODO)"

class FetchIdentities(ldapclient.LDAPClient):
    factory = None

    def connectionLost(self, reason):
	ldapclient.LDAPClient.connectionLost(self, reason)

    def connectionMade(self):
        ldapclient.LDAPClient.connectionMade(self)
	d=self.bind()
	d.addCallback(self._handle_bind_success)

    def _handle_bind_success(self, x):
	matchedDN, serverSaslCreds = x
        self.factory._wakeup_protocol()

    def _cbFetch(self, results):
        if not results:
	    raise ldaperrors.LDAPUnknownError(ldaperrors.other, "unauthorized")
        elif len(results) > 1:
            raise 'GOT DUPLICATES (TODO)'
        else:
            return results[0].dn

    def fetch(self, name):
        try:
            o=ldapsyntax.LDAPEntry(client=self,
                                   dn=name)
            d=o.search(scope=pureldap.LDAP_SCOPE_baseObject,
                       typesOnly=1,
                       sizeLimit=2)
            d.addCallbacks(callback=self._gotIdentityDN,
                           callbackArgs=(name,),
                           errback=self.search,
                           errbackArgs=(name,))
        except:
            d = self.search(None, name)
	return d

    def search(self, fail, name):
	filter=None
	try:
	    filter=ldapfilter.parseFilter(name)
	except ldapfilter.InvalidLDAPFilter:
	    try:
		filter=ldapfilter.parseFilter('('+name+')')
	    except ldapfilter.InvalidLDAPFilter:
		if self.factory.ldapFilterTemplate is not None:
		    try:
			filter=ldapfilter.parseFilter(
			    self.factory.ldapFilterTemplate
			    % {'name':name})
		    except ldapfilter.InvalidLDAPFilter:
			pass

	base=self.factory.ldapbase
	if self.factory.ldapFilterBase is not None:
	    base=self.factory.ldapFilterBase

	deferred=defer.Deferred()
	if filter is None:
	    deferred.errback(Failure('No such Identity'))
	else:
	    LDAPSearchIdentity(deferred=deferred,
			       client=self,
			       baseObject=base,
			       filter=filter)
	    deferred.addCallbacks(callback=self._gotIdentityDN,
				  callbackArgs=(name,),
				  errback=self.search)
	return deferred

    def _gotIdentityDN(self, dn, name):
	ident = LDAPIdentity(name, dn, self.factory.authorizer,
			     LDAPAuthFactory,
			     self.factory.serviceLocationOverride)
	#TODO I don't want to enumerate them here!
	ident.addKeyByString("edit", name)
	return ident

class FetchIdentitiesFactory(protocol.ReconnectingClientFactory):
    protocol = FetchIdentities
    maxDelay = 60

    def __init__(self,
		 ldapbase,
		 serviceLocationOverride,
		 ldapFilterTemplate=None,
		 ldapFilterBase=None,
		 authorizer=None):
	assert authorizer
	self.authorizer = authorizer
	self.ldapbase = ldapbase
	self.serviceLocationOverride = serviceLocationOverride
	self.ldapFilterTemplate = ldapFilterTemplate
	self.ldapFilterBase = ldapFilterBase
	self.requests = []
	self.proto = None

    def __getstate__(self):
        r={}
        r.update(self.__dict__)
        r['proto'] = None
        r['_callID'] = None
        return r

    def buildProtocol(self, addr):
	self.resetDelay()
	assert not self.proto
	self.proto=protocol.ClientFactory.buildProtocol(self, addr)
	self._wakeup_protocol()
	return self.proto

    def _wakeup_protocol(self):
        # TODO this should be self.proto.bound, don't want to send a
        # request before the bind.
	if self.proto and self.proto.connected:
	    while self.requests:
		name, deferred = self.requests.pop(0)
		d = self.proto.fetch(name)
		d.chainDeferred(deferred)

    def fetch(self, name):
	deferred=defer.Deferred()
	self.requests.append((name, deferred))
	self._wakeup_protocol()
	return deferred

    def clientConnectionFailed(self, connector, reason):
	self.proto = None
	protocol.ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
	self.proto = None
	protocol.ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def stopFactory(self):
	self.stopTrying()

class LDAPAuthorizer(Authorizer):
    def __init__(self, ldapbase, serviceLocationOverride=None,
		 ldapFilterTemplate='(|(cn=%(name)s)(uid=%(name)s))',
		 ldapFilterBase=None,
		 ):
        Authorizer.__init__(self)
	self.ldapbase = ldapbase
	self.serviceLocationOverride = serviceLocationOverride
	self.ldapFilterTemplate = ldapFilterTemplate
	self.ldapFilterBase = ldapFilterBase
	self.pendingIdentityRequests = {}
	self.fetcher = FetchIdentitiesFactory(
	    ldapbase=self.ldapbase,
	    serviceLocationOverride=self.serviceLocationOverride,
	    ldapFilterTemplate=self.ldapFilterTemplate,
	    ldapFilterBase=self.ldapFilterBase,
	    authorizer=self,
	    )

	dn = distinguishedname.DistinguishedName(stringValue=ldapbase)
	c = ldapconnector.LDAPConnector(reactor, dn, self.fetcher,
					overrides=serviceLocationOverride)
	c.connect()

    def getIdentityRequest(self, name):
	"""Get a Deferred callback registration object.

	I return a deferred (twisted.internet.defer.Deferred) which will
	be called back to when an identity is discovered to be available
	(or errback for unavailable).  It will be returned unarmed, so
	you must arm it yourself.
	"""

	req = self.fetcher.fetch(name)
	return req

class LDAPService(Service):
    def _ident_ok(self, ident, name, req):
	req.callback(self.createPerspective(name))

    def _ident_fail(self, msg, name, req):
	req.errback("No such perspective %s: %s"%(name, msg))

    def loadPerspective(self, name):
	identreq = self.authorizer.getIdentityRequest(name)
	req = defer.Deferred()
	identreq.addCallback(self._ident_ok, name, req)
	identreq.addErrback(self._ident_fail, name, req)
	identreq.arm()
	return req

    def getPerspectiveRequest(self, name):
	try:
	    p = self.getPerspectiveNamed(name)
	except KeyError:
	    return self.loadPerspective(name)
	else:
	    return defer.succeed(p)
