# See https://stackoverflow.com/questions/32815451/are-global-variables-thread-safe-in-flask-how-do-i-share-data-between-requests
from flask_caching import Cache

# Instantiate the cache
cache = Cache()
