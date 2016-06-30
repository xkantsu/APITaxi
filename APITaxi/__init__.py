# -*- coding: utf-8 -*-
VERSION = (0, 1, 0)
__author__ = 'Vincent Lara'
__contact__ = "vincent.lara@data.gouv.fr"
__homepage__ = "https://github.com/"
__version__ = ".".join(map(str, VERSION))

from flask import Flask, request_started, request_finished
import os
from flask.ext.dogpile_cache import DogpileCache

def create_app(sqlalchemy_uri=None):
    from .extensions import redis_store, redis_store_haillog, user_datastore
    app = Flask(__name__)
    app.config.from_object('APITaxi.default_settings')
    if 'APITAXI_CONFIG_FILE' in os.environ:
        app.config.from_envvar('APITAXI_CONFIG_FILE')
    if not 'ENV' in app.config:
        app.logger.error('ENV is needed in the configuration')
        return None
    if app.config['ENV'] not in ('PROD', 'STAGING', 'DEV'):
        app.logger.error("""Here are the possible values for conf['ENV']:
        ('PROD', 'STAGING', 'DEV') your's is: {}""".format(app.config['env']))
        return None
    #Load configuration from environment variables
    for k in app.config.keys():
        if not k in os.environ:
            continue
        app.config[k] = os.environ[k]
    if sqlalchemy_uri:
        app.config['SQLALCHEMY_DATABASE_URI'] = sqlalchemy_uri

    from APITaxi_models import db
    db.init_app(app)
    redis_store.init_app(app)
    redis_store.connection_pool.get_connection(0).can_read()
    redis_store_haillog.init_app(app)
    redis_store_haillog.connection_pool.get_connection(0).can_read()
    from . import api
    api.init_app(app)

    from APITaxi_utils.version import check_version, add_version_header
    request_started.connect(check_version, app)
    request_finished.connect(add_version_header, app)

    from flask.ext.uploads import configure_uploads
    from .api.extensions import documents
    configure_uploads(app, (documents,))
    from APITaxi_utils.login_manager import init_app as init_login_manager
    init_login_manager(app, user_datastore, None)

    from . import tasks
    tasks.init_app(app)

    from APITaxi_models import security
    user_datastore.init_app(db, security.User, security.CachedUser,
            security.Role)
    cache = DogpileCache()
    cache.init_app(app)

    @app.before_first_request
    def warm_up_redis():
        not_available = set()
        available = set()
        cur = db.session.connection().connection.cursor()
        cur.execute("""
        SELECT taxi.id AS taxi_id, vd.status, vd.added_by FROM taxi
        LEFT OUTER JOIN vehicle ON vehicle.id = taxi.vehicle_id
        LEFT OUTER JOIN vehicle_description AS vd ON vehicle.id = vd.vehicle_id
        """)
        users = {u.id: u.email for u in security.User.query.all()}
        for taxi_id, status, added_by in cur.fetchall():
            user = users.get(added_by)
            taxi_id_operator = "{}:{}".format(taxi_id, user)
            if status == 'free':
                available.add(taxi_id_operator)
            else:
                not_available.add(taxi_id_operator)
        to_remove = list()
        if redis_store.type(app.config['REDIS_NOT_AVAILABLE']) != 'zset':
            redis_store.delete(app.config['REDIS_NOT_AVAILABLE'])
        else:
            cursor, keys = redis_store.zscan(app.config['REDIS_NOT_AVAILABLE'], 0)
            keys = set([k[0] for k in keys])
            while cursor != 0:
                to_remove.extend(keys.intersection(available))
                not_available.difference_update(keys)
                cursor, keys = redis_store.zscan(app.config['REDIS_NOT_AVAILABLE'],
                        cursor)
                keys = set([k[0] for k in keys])
        if len(to_remove) > 0:
            redis_store.zrem(app.config['REDIS_NOT_AVAILABLE'], to_remove)
        if len(not_available) > 0:
            redis_store.zadd(app.config['REDIS_NOT_AVAILABLE'], **{k:0 for k in not_available})

    from APITaxi_models.hail import HailLog
    def delete_redis_keys(response):
        from flask import g
        if not hasattr(g, 'keys_to_delete'):
            return response
        redis_store.delete(*g.keys_to_delete)
        return response

    app.after_request_funcs.setdefault(None, []).append(
            HailLog.after_request(redis_store_haillog)
    )
    app.after_request_funcs.setdefault(None, []).append(
            delete_redis_keys
    )
    return app
