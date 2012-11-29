"""
Utility functions for interacting with our Git repos
"""
from webapp import app
from flask import abort, flash
from datetime import datetime, timedelta
import json
from sqlalchemy import func, desc
from models import *
from git import Repo
from git.exc import *
from requests.exceptions import HTTPError
import requests
import os
import humanize


basename = os.path.dirname(__file__)
now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S")
repo = Repo(os.path.join(basename, "git_dir"))


def ok_to_update():
    """ If we've got more than two new addresses, or a day's gone by """
    counter = Counter.query.first()
    if not counter:
        counter = Count(0)
        db.session.add(counter)
        db.session.commit()
    elapsed = counter.timestamp - datetime.now()
    return any([counter.count >= 2, elapsed.days >= 1])

def check_if_exists(address):
    """
    Check whether a submitted address exists in the DB, add it if not,
    re-generate the spammers.txt file, and open a pull request with the updates
    """
    normalised = "@" + address.lower().strip()
    # add any missing spammers to our DB
    update_db()
    if not Address.query.filter_by(address=normalised).first():
        db.session.add(Address(address=normalised))
        count = Counter.query.first()
        if not count:
            count = Counter(0)
        count.count += 1
        db.session.add(count)
        db.session.commit()
        write_new_spammers()
        return False
    return True

def write_new_spammers():
    """ Synchronise all changes between GitHub and webapp """
    errs = False
    if ok_to_update():
        # re-generate spammers.txt
        with open(os.path.join(basename, "git_dir", 'spammers.txt'), 'w') as f:
            updated_spammers = " OR \n".join([addr.address for
                addr in Address.query.order_by('address').all()])
            f.write(updated_spammers)
            # files under version control should end with a newline
            f.write(" \n")
        # add spammers.txt to local repo
        index = repo.index
        try:
            index.add(['spammers.txt'])
            commit = index.commit("Updating Spammers on %s" % now)
            # push local repo to webapp's remote
            our_remote = repo.remotes.our_remote
            our_remote.push('master')
        except GitCommandError as e:
            errs = True
            app.logger.error("Couldn't push to staging remote. Err: %s" % e)
        # send pull request to main remote
        our_sha = "urschrei:master"
        their_sha = 'master'
        if not errs and pull_request(our_sha, their_sha):
            # reset counter to 0
            counter = Counter.query.first()
            counter.count = 0
            counter.timestamp = func.now()
            db.session.add(counter)
            db.session.commit()
        else:
            # register an error
            errs = True
        if errs:
            flash(
                "There was an error sending your updates to GitHub. We'll \
try again later, though, and they <em>have</em> been saved.", "text-error"
                )

def get_spammers():
    """ Return an up-to-date list of spammers from the main repo text file """
    with open(os.path.join(basename, "git_dir", 'spammers.txt'), 'r') as f:
        spammers = f.readlines()
    # trim the " OR" and final newline from the entries
    # FIXME: this is a bit fragile
    return [spammer.split()[0] for spammer in spammers]

def pull_request(our_sha, their_sha):
    """ Open a pull request on the main repo """
    payload = {
        "title": "Updated Spammers on %s" % now,
        "body": "Updates from the webapp",
        "head": our_sha,
        "base": their_sha
    }
    headers = {
        "Authorization": 'token %s' % app.config['GITHUB_TOKEN'],
    }
    req = requests.post(
        "https://api.github.com/repos/drcongo/spammy-recruiters/pulls",
        data=json.dumps(payload), headers=headers)
    try:
        req.raise_for_status()
    except HTTPError as e:
        app.logger.error("Couldn't open pull request. Error: %s" % e)
        return False


def checkout():
    """ Ensure that repos are in sync, and we have the correct spammers.txt """
    git = repo.git
    origin = repo.remotes.origin
    our_remote = repo.remotes.our_remote
    repo.heads.master.checkout()
    index = repo.index
    try:
        # fetch all remote refs
        git.pull(all=True)
        # ensure that local master is in sync with our_remote
        if index.diff('our_remote/master'):
            # unmerged PRs or failed pushes to origin, sync with our_remote
            our_remote.pull('master')
            our_remote.push('master')
        git.checkout("spammers.txt")
    except (GitCommandError, CheckoutError) as e:
        # Not much point carrying on without the latest spammer file
        app.logger.critical("Couldn't check out latest spammers.txt: %s" % e)
        abort(500)

def update_db():
    """ Add any missing spammers to our app DB """
    # pull changes from main remote into local
    checkout()
    their_spammers = set(get_spammers())
    our_spammers = set(addr.address.strip() for addr in
        Address.query.order_by('address').all())
    to_update = [Address(address=new_addr) for new_addr in
        list(their_spammers - our_spammers)]
    db.session.add_all(to_update)
    # reset sync timestamp
    latest = UpdateCheck.query.first() or UpdateCheck()
    latest.timestamp = func.now()
    db.session.add(latest)
    db.session.commit()

def sync_check():
    """
    Syncing the local and remote repos is a relatively slow process;
    there's no need to do it more than once per hour, really
    """
    latest = UpdateCheck.query.first()
    if not latest:
        latest = UpdateCheck()
        db.session.add(latest)
        db.session.commit()
    elapsed = datetime.now() - latest.timestamp
    if elapsed.seconds > 3600:
        update_db()
        elapsed = datetime.now() - timedelta(seconds=1)
    return humanize.naturaltime(elapsed)