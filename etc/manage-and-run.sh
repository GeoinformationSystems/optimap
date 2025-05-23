#!/bin/bash
set -e

# Collect static files
echo "OPTIMAP ENTRYPOINT | Collect static files"
python manage.py collectstatic --noinput

# Create a cache table
echo "OPTIMAP ENTRYPOINT | Create cache table"
python manage.py createcachetable

# Apply database migrations
echo "OPTIMAP ENTRYPOINT | Apply database migrations"
python manage.py migrate

# Start server
echo "OPTIMAP ENTRYPOINT | Starting server"
# make the next process the main process, cf. https://www.baeldung.com/ops/docker-exec-process-replacement
exec python manage.py runserver 0.0.0.0:8000

# TODO: revisit entrypoint approach if there are any issues, e.g., by using an extra migration container, cf. https://stackoverflow.com/questions/33992867/how-do-you-perform-django-database-migrations-when-using-docker-compose, or by manually running selected commands after installation, cf. https://www.baeldung.com/ops/django-database-migrations-docker-compose
