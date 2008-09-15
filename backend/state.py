#!/usr/bin/python

# Class and associated files for representing the state of a
# Yeah-But.

# Persistent information will be stored in the databes although this class
# will facilitate caching requests.

# $Id: state.py 100 2008-04-11 00:34:48Z paul $

from datetime import datetime
import web # Needed for database access
from log import *
import re
import random # Needed for random cluster assignment hack.  Should disappear

import search

class DataError(Exception):
  def __init__(self, value):
    self.value = value
  def __str__(self):
    return repr(self.value)


_name_validator = re.compile(r'^[\w.-]{1,32}$')
def _validate_name(name):
  if not _name_validator.match(name):
    raise DataError("Invalid username: '%s'.  Usernames must be no more than 32 characters, and consist solely of alphanumerics, underscore, period, and dash" % name)

_email_validator = re.compile(r'[^@]+@[a-z-]+([.][a-z-]+)+', re.I)
def _validate_email_address(email):
  if len(email) > 64: raise DataError("Invalid email address: '%s'.  We can only store 64 characters of email address.")
  if not _email_validator.match(email):
    raise DataError("Invalid email address: '%s'.  We won't be able to figure out how to send mail to that address.")
#TODO: check for common mistakes that might not be mistakes in Javascript
#_probable_email_address = re.compile(r'[\w.+-]+@[a-z-]+([.][a-z-]+)*[.][a-z]{2,4}', 'I')
_pwd_validator = re.compile(r'^[^%]{4,64}$')  #TODO: permit '%'s when web.py can deal with it
def _validate_password(pwd):
  if not _pwd_validator.match(pwd):
    raise DataError("Invalid password.  Passwords must be between 4 and 64 characters, and not contain the character '%'")


#TODO: catch MySQL exceptions and raise custom ones.
  
class State:
    def __init__(self, database='yb'):
        log_dbg('Initializing state (%s) . . .' % database)
        
        random.seed()

        # as MySQL root: GRANT ALL ON yb.* to 'yeahbut'@'localhost';
        web.config.db_parameters = dict(
            dbn='mysql',
            user='yeahbut',
            host='localhost',
            pw='',
            db=database)

        web.load() #We want to use the DB before web.py starts totally

        #self.ssh_client = paramiko.SSHClient()
        self.cluster_size_cache = {}

        self.search = search.Search(database)

        try:
          self.active_cid = web.query('select active_cid from globals')[0].active_cid;
        except IndexError:
          self.active_cid = None #presumably, the first thing we'll do is clear the DB

    #mainly for testing, when we need to repeatedly make sane states
    def close(self):
      self.search.close()

      
    # For use when web server is NOT running
    def connect(self, database='yb'):
        #web.load() #already done above
        pass

    def clear(self): #nuke everything in the database
        for table in ['user', 'ticket', 'post', 'post_content', 'vote',
                      'cluster', 'cluster_connection', 'relevance',
                      'callout_votes', 'history', 'globals']:
            web.query('truncate %s' % table)

        web.query('insert into globals values (active_cid=0)')
        self.active_cid = 0

        #temporary hack -- all clusters are connected
        for a in xrange(0,5):
            for b in xrange(0,5):
                web.query('insert into cluster_connection values (%d, %d)' % (a,b))

        self.search.clear()


    #TODO: impose restrictions on name contents 
    #TODO: don't store password directly!
    #NOTE: MySQL is case-insensitive by default.  Currently, we follow this convention in names.
    def create_user(self, name, pwd, email):
      _validate_name(name)
      _validate_password(pwd)
      _validate_email_address(email)
      fakecid = random.randint(0,4) #TODO temporary hack
      new_user = int(web.insert('user', name=name, password=pwd, 
                                email=email, cid0=fakecid, cid1=fakecid,
                                current_batch=1))
      return new_user

    def check_ticket(self, uid, ticket):
        if len(web.select('ticket', where='uid=%d and ticket=%s' % (uid, web.sqlquote(ticket)))) == 0:
            return False
        else:
            web.update('ticket', where='uid=%d and ticket=%s' %
                       (uid, web.sqlquote(ticket)),
                       last_used=datetime.now().isoformat())
            return True

    def make_ticket(self, uid):
        ticket = str(random.randint(0, 2**128))
        #web.insert('ticket', uid=uid, ticket=ticket,
        #           last_used=datetime.now().isoformat())

        web.query('insert into ticket (uid, ticket) values (%d, %s)'  % (uid, web.sqlquote(ticket)))
        return ticket
    
    def get_user(self, uid):
        user = web.query('select *, cid%d as cid from user where id=%d' % (self.active_cid, uid))
        if len(user) is 0: raise DataError("user with uid %d not found" % uid)

        return user[0]

    def get_uid_from_name(self, name):
        user = web.select('user', where='name=%s' % web.sqlquote(name))
        if len(user) is 0: raise DataError("user with name %s not found" % name)
        return int(user[0].id)

    
    def create_post(self, uid, claim, content):
        b_s = 0 #TMP -- should probably leave out from the SQL query
        b_s += 2 * content.count('iddqd') #testing purposes only...
        
        pid = int(web.insert('post', uid=uid,
                                  claim=claim,
                                  broad_support=b_s))
        tokens = ' '.join(self.search.tokens(content))
        web.insert('post_content', pid=pid,
                   #these args are automatically sqlquoted?
                   raw=content,  
                   tokens = tokens,
                   safe_html='TODO')
        self.vote(uid, pid)
        self.search.add_article_contents(tokens, pid, b_s)
        return pid

    #TODO: this laziness system works, but is kinda ugly.  We should fix it.
    def get_post(self, pid, content=False):
        post = web.select('post', where='id=%d' % pid)
        
        if len(post) is 0: raise DataError("article with pid %d not found" % pid)
        
        if content == False:
            return post[0]

        lazies = {
            'raw' : (lambda : web.query('select raw from post_content where pid=%d' % pid)[0].raw),
            'safe_html' : (lambda : web.query('select safe_html from post_content where pid=%d' % pid)[0].safe_html),
            'tokens' : (lambda : web.query('select tokens from post_content where pid=%d' % pid)[0].tokens)
            }
            
        lazies.update(post[0])
        
        return web.storage(lazies)

    def inc_batch(self, user):
        web.query('update user set current_batch=%d where id=%d'
                  % (user.current_batch+1, user.id))

    def add_to_history(self, uid, pid, batch, position):
        web.insert('history', uid=uid, pid=pid, batch=batch, position=position)

    def get_history(self, uid, batch):
        pids = [s.pid for s in
                web.query('select pid from history where uid=%d and batch=%d order by position'
                         % (uid, batch))]

        return [self.get_post(pid, True) for pid in pids]
        

    #TODO: after get_post is fixed, optimize, by going back to grabbing them.
    def get_random_pids(self, count):
        return [p.id for p in web.select('post', order='rand()', limit='%d' % count)]

    def vote(self, uid, pid, term=None):
        if len(web.select('vote', where='uid=%d and pid=%d' % (uid, pid))) == 0:
            web.insert('vote', uid=uid, pid=pid)
        # otherwise vote already exists -- do nothing

        #TODO: validate term
        if term != None  and  len(web.select('relevance', where='uid=%d and pid=%d and term=%s' % (uid, pid, web.sqlquote(term)))) == 0:
            #log_dbg("STATE: vote term: [%s]" % term) #terms are bare
            web.insert('relevance', uid=uid, pid=pid, term=web.sqlquote(term))

    def voted_for(self, uid, pid):
        return len(web.select('vote', where='uid=%d and pid=%d' % (uid, pid))) > 0

    def update_support(self, pid, support):
        web.update('post', where='id=%d' % pid, broad_support=support)
        self.search.update_support(pid, support)

    def dump_votes(self):
        votes = web.select('vote')
        return [(vote.uid, vote.pid) for vote in votes]

    def votes_by_uid(self, uid):
        votes = web.select('vote', where = 'uid=%d' % uid)
        return votes

    def recent_votes(self, voter, limit):
        return [v.pid for v in
            web.query(
              '''select vote.pid from vote
                   where uid=%d
                   order by date_voted desc
                   limit %d''' % (voter, limit))]

    def recent_unviewed_votes(self, voter, viewer, limit):
        #Get votes made by _voter_ for articles _viewer_ hasn't viewed.
        return [v.pid for v in
            web.query(
              '''select vote.pid from vote
                   left outer join history
                     on (vote.pid=history.pid and history.uid=%d)
                   where vote.uid=%d and history.uid is null
                   order by vote.date_voted desc
                   limit %d''' % (viewer, voter, limit))]
        # the weird join is there because we care about the *absence* of an appropriate _history_ entry


    def votes_for_pid(self, pid):
        votes = web.select('vote', where = 'pid=%d' % pid) 
        if len(votes) is 0: return []
        return votes

    def get_num_clusters(self):
        num_query = web.select('vote', what='count(*)')
        num_clusters = num_query[0]['count(*)']
        return num_clusters

    def get_sample_users_in_cluster(self, cluster, count):
        return web.query('select *, cid%d as cid from user where cid%d=%d order by rand() limit %d'
                  % (self.active_cid, self.active_cid, cluster, count) )

    def apply_clusters(self, assignments):
        for uid, cluster in assignments:
            self._setcluster(uid, cluster)

    def get_cluster_by_uid(self, uid):
        return self.get_user(uid).cluster

    def get_cluster_size(self, cluster):
        #maybe setdefault is better, but I couldn't get it to work.  PS
        if self.cluster_size_cache.has_key(cluster):
            return self.cluster_size_cache[cluster]
        else:
            rec = int(web.select('user', what='count(*)',
                          where='cid%d=%d' % (self.active_cid, cluster)
                     )[0]['count(*)'])
            self.cluster_size_cache[cluster] = rec
            return rec

    def get_votes_by_pid_clustered(self, pid):
        self.cluster_count = 5 #TODO update with each rebasing, not here
        
        vote_list = web.query('''
        select user.cid%d as cid, count(*) from vote
        inner join user on vote.uid=user.id
        where pid=%d
        group by cid
        ''' % (self.active_cid, pid))
        retval = [0 for c in xrange(self.cluster_count)]
        for record in vote_list:
            retval[record['cid']] = record['count(*)']
        return retval

    def get_term_popularity(self, term):
        log_tmp("STATE: term: [%s]" % term)
        return web.query('select count(*) from relevance where term="%s"' % web.sqlquote(term))[0]['count(*)']

    def get_term_in_clusters(self, term, clusters):
        log_tmp("STATE: clusters: %d" % clusters)
        #It looks like keyword arguments (like those used in web.insert in _vote_())
        #get quoted twice, if used in combination with sqlquote()
        #TODO: force web.py to do it right, rather than matching them
        return [r.pid for r in
            web.query('''
                select relevance.pid from relevance
                inner join user on relevance.uid=user.id
                where relevance.term="%s" and user.cid%d in (%s)
                ''' % (
                       web.sqlquote(term),
                       self.active_cid,
                       ', '.join(['%d'%c for c in clusters]))
                      )]

    

    #clusters are in a graph, defined by _connections_
#    def _process_connection_list(self, graph):
#        for pair in graph:
#            self._add_connection(pair[0], pair[1])
#    
#    def _add_connection(self, clust1, clust2):
#        self.connections[clust1].append(clust2)
#        self.connections[clust2].append(clust1)
#

    def connected_clusters(self, cluster):
        return [con.cid_to for con in
           web.query('''select cid_to from cluster_connection
                        where cid_from=%d''' % (cluster))]
  
    def connected_cluster_sample(self, cluster, count):
        return [con.cid_to for con in
           web.query('''select cid_to from cluster_connection
                        where cid_from=%d
                        order by rand()
                        limit %d''' % (cluster, count))]
    
    def add_callout(self, pid, start, end, voter):
        web.query('insert into callout_votes values (%d, %d, %d, %d)' 
                  % (pid, start, end, voter))
        #TODO handle repeat votes -- update?

    def callouts_for(self, pid):
        return web.query('select * from callout_votes where pid=%d' % pid)


the = State()
