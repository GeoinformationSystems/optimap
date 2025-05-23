# OPTIMAP

[![Project Status: WIP – Initial development is in progress, but there has not yet been a stable, usable release suitable for the public.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip) [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.8198944.svg)](https://doi.org/10.5281/zenodo.8198944)

Geospatial discovery of research articles based on open metadata.
The OPTIMETA Portal is part of the OPTIMETA project (<https://projects.tib.eu/optimeta>) and relies on the spatial and temporal metadata collected for scientific papers with the OPTIMETA Geo Plugin for Open Journal Systems ([OJS](https://pkp.sfu.ca/ojs/)) published at <https://github.com/TIBHannover/optimetaGeo>.
The product name of the portal is OPTIMAP.
The development is continued in the project KOMET (<https://projects.tib.eu/komet>).

The OPTIMAP has the following features:

- Start page with a full screen map (showing geometries and metadata) and a time line of the areas and time periods of interest for scientific publications
- Passwordless login via email
- RESTful API at `/api`

OPTIMAP is based on [Django](https://www.djangoproject.com/) (with [GeoDjango](https://docs.djangoproject.com/en/4.1/ref/contrib/gis/) and [Django REST framework](https://www.django-rest-framework.org/)) with a [PostgreSQL](https://www.postgresql.org/)/[PostGIS](https://postgis.net/) database backend.

The development of OPTIMAP was and is supported by the projects [OPTIMETA](https://projects.tib.eu/optimeta/en/) and [KOMET](https://projects.tib.eu/komet/en/).

[![OPTIMETA Logo](https://projects.tib.eu/fileadmin/_processed_/e/8/csm_Optimeta_Logo_web_98c26141b1.png)](https://projects.tib.eu/optimeta/en/) [![KOMET Logo](https://projects.tib.eu/fileadmin/templates/komet/tib_projects_komet_1150.png)](https://projects.tib.eu/komet/en/)

## Configuration

All configuration is done via the file `optimap/settings.py`.
Configurations that need to be changed for different installations and for deployment are also exposed as environment variables.
The names of these environment variables start with `OPTIMAP_`.
The settings files loads these from a file `.env` stored in the same location as `settings.py`, or from the environment the server is run it.
A complete list of existing parameters is provided in the file `optimap/.env.example`.

## Run with Docker

The project is containerized using Docker, with services defined in `docker-compose.(deploy.)yml`. To start all services, run:

```bash
docker compose up

docker compose run --entrypoint python app manage.py loaddata fixtures/test_data.json
```

The database migrations are applied as part of the startup script, see file `etc/manage-and-run.sh`.
You can still run the commands below manually if need be, e.g., during development.

```bash
docker compose run --entrypoint python app manage.py makemigrations # should not detect and changes, otherwise your local config might be outdated
docker compose run --entrypoint python app manage.py migrate
docker compose run --entrypoint python app manage.py collectstatic --noinput
```

Now open a browser at <http://localhost:80/>.

### Services Overview

- db: Runs a PostgreSQL database with PostGIS extensions. Data is persisted in a Docker volume named db_data.
- app: Our primary Django web application.
- webserver: An Nginx server for serving static files and test files.

### Ports

Not all of these ports are exposed by default, but they are available for local development - just uncomment the matching lines in the `docker-compose.yml` file.

- `5432`: Database (PostgreSQL/PostGIS)
- `8000`: App (Django server)
- `80`: Webserver (Nginx)

## Development

### Test Data

The folder `/fixtures` contains some test data, which can be used to populate the database during development. These data are provided either as SQL commands for insertion into the database or as database dumps that can be loaded using [`django-admin`](https://docs.djangoproject.com/en/dev/ref/django-admin/).

### Managing Test Data

#### Creating Test Data Dumps

To create a data dump after generating or harvesting test data, use the following command:

```bash
python manage.py dumpdata --exclude=auth --exclude=contenttypes | jq > fixtures/test_data.json
```

#### Loading Test Data

To load the test data into your database, run:

```bash
python manage.py loaddata fixtures/test_data.json
```

#### Adding New Test Data

If additional test data is required, you can:

- Copy and paste existing records.
- Add new records via the Django admin interface (preferred method).
- Manually modify the test data JSON file.

Tools for Geometries
For creating or editing geometries, consider using the tool [WKTMap](https://wktmap.com/), which provides an easy way to generate Well-Known Text (WKT) representations of spatial data.

### Run Locally

1. Create a `.env` file based on `.env.example` in the same directory where `settings.py` resides, and fill in the configuration settings as needed.
2. Run the commands below to set up and run the project locally. This setup uses the built-in Python [`venv`](https://docs.python.org/3/library/venv.html). Refer to [this tutorial](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/#create-and-use-virtual-environments) for more details.

```bash
# Create a virtual environment (once only)
python -m venv .venv

# Activate the virtual environment
source .venv/bin/activate

# Confirm Python path
which python

# Install GDAL
gdalinfo --version

# Install gdal Pyhton library matching your GDAL version
pip install gdal=="$(gdal-config --version).*"

# Install Python dependencies
pip install -r requirements.txt

# create local DB container (once)
# docker run --name optimapDB -p 5432:5432 -e POSTGRES_USER=optimap -e POSTGRES_PASSWORD=optimap -e POSTGRES_DB=optimap -d postgis/postgis:14-3.3
# get a clean one later: docker rm -f optimapDB

# Start the database container
docker start optimapDB

# Apply database migrations
python manage.py makemigrations
python manage.py migrate

# Create a cache table
python manage.py createcachetable

# Collect static files
python manage.py collectstatic --noinput

# If you need to run tasks (harvesting, data export) then start a cluster in a separate shell
python manage.py qcluster

# Start the Django development server
python manage.py runserver

# Start the app with specific configurations for development
OPTIMAP_CACHE=dummy OPTIMAP_DEBUG=True python manage.py runserver
```

Now open a browser at <http://127.0.0.1:8000/>.

#### Additional Setup

- Creating a Superuser: Refer to the instructions below to create a superuser for accessing the admin interface.
- Loading Test Data: Refer to the instructions above to load test data into the database.

#### Shutting Down

When finished, deactivate the virtual environment and stop the database container:

```bash
deactivate

docker stop optimapDB
```

#### Debug Mode Configuration

By default, `OPTIMAP_DEBUG` is now set to `False` to ensure a secure and stable production environment. If you need to enable debug mode for development purposes, explicitly set the environment variable in your `.env` file or pass it as an argument when running the server.

#### Enable Debug Mode for Development

To enable debug mode, add the following to your `.env` file:

```env
OPTIMAP_DEBUG=True
```

### Debug with VS Code

Select the Python interpreter created above (`optimap` environment), see instructions at <https://code.visualstudio.com/docs/python/tutorial-django> and <https://code.visualstudio.com/docs/python/environments>.

**Note:** The `docker-compose.yml` file is intended for **development**, not deployment.

Configuration for debugging with VS Code:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: Django Run",
      "type": "python",
      "request": "launch",
      "program": "${workspaceFolder}/manage.py",
      "args": ["runserver"],
      "env": {
        "OPTIMAP_DEBUG": "True",
        "OPTIMAP_CACHE": "dummy"
      },
      "django": true,
      "justMyCode": true
    }
  ]
}
```

### Debug email sending

Add `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` to the `.env` file to have emails printed to the console instead of sent via SMTP.

Alternatively, you can run a local STMP server with the following command and configuration:

```bash
python -m smtpd -c DebuggingServer -n localhost:5587
```

```env
OPTIMAP_EMAIL_HOST=localhost
OPTIMAP_EMAIL_PORT=5587
```

### Create Superusers/Admin

Superusers or administrators can be created using the `createsuperuser` command. This user will have access to the Django admin interface.

```bash
python manage.py createsuperuser --username=optimap --email=nomail@optimap.science
```

You will be prompted for a password. After entering one, the superuser will be created immediately. If you omit the --username or --email options, the command will prompt you for those values interactively.

Access the admin interface at <http://127.0.0.1:8000/admin/>.

#### Running in a Dockerized App

To create a superuser within the containerized application, use the following command:

```bash
docker-compose run web python manage.py createsuperuser
```

This will run the same process as above but within the Docker environment. Ensure the container is running and accessible before executing this command

### Run tests

See <https://docs.djangoproject.com/en/4.1/topics/testing/overview/> for testing Django apps.

UI tests are based on [Helium](https://github.com/mherrmann/selenium-python-helium) (because [Pylenium](https://github.com/ElSnoMan/pyleniumio) would need pytest in addition).

```bash
pip install -r requirements-dev.txt
```

```bash
python manage.py test tests

# show deprecation warnings
python -Wa manage.py test

# configure logging level for cleaner test progress output
OPTIMAP_LOGGING_LEVEL=WARNING python manage.py test tests

# running UI tests needs either compose configuration or a manage.py runserver in a seperate shell
docker-compose up --build

python -Wa manage.py test tests-ui
```

### Develop tests

For developing the UI tests, you can remove the `headless=True` in the statements for starting the browsers so you can "watch along" and inspect the HTML when a breakpoint is hit as the tests are executed.

### Debug tests with VS Code

A configuration to debug the test code and also print deprecation warnings:

```json
{
  "name": "Python: Django Test",
  "type": "python",
  "request": "launch",
  "pythonArgs": ["-Wa"],
  "program": "${workspaceFolder}/manage.py",
  "args": ["test", "tests"],
  "env": {
    "OPTIMAP_DEBUG": "True"
  },
  "django": true,
  "justMyCode": true
}
```

Change the argument `tests` to `tests-ui` to run the UI tests.

See also documentation at <https://code.visualstudio.com/docs/python/tutorial-django>.

### Issues during development

- If you get a message during login that there is an issue with the CSRF token, e.g. `WARNING:django.security.csrf:Forbidden (CSRF token from POST incorrect.): /loginres/` in the log and also i nthe UI, then switch to using `localhost:8000` as the domain, not the localhost IP used in the examples in this README file.

## Contributing

_All contributions are welcome!_
We appreciate any feedback, suggestions, or code contributions to improve the project.
Please follow the [contribution guidelines](CONTRIBUTING.md) for more details.

## Changelog

We operate a [changelog](CHANGELOG.md) to keep track of changes and updates to the project.
The changelog follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and is versioned according to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is managed in `optimap/__init__.py`.

## Design colours and logos

Optimeta colour = _primary colour_: #158F9B

Complimentary colour for _warnings_, _errors_: #9B2115

Colours for _highlighting_ (split complimentary): #3C159B #9B7115

For future use, optional colours for variation, e.g., for different map features: #158F9B #159B71 #159B8C #158F9B #15749B #15599B

The **logos** and favicon are in the repository in the folder [`publications/static/`](https://github.com/nuest/optimap/tree/main/publications/static).

## Deploy

~~Deploy using `docker-compose` or see [`fly.io.md`](fly.io.md) for notes on deploying to Fly.io.~~

## Operation

### Block Emails/Domains

#### What It Does

- Blocks specific emails and entire domains from registering.
- Prevents login attempts from blocked users.
- Admin can delete users and instantly block their email/domain.

#### How to Use in Django Admin

1. **Manually Add Blocked Emails/Domains**
   - Go to `/admin/`
   - Add emails in **Blocked Emails** or domains in **Blocked Domains**.
2. **Block Users via Admin Action**
   - Go to `/admin/auth/user/`
   - Select users → Choose **"Delete user and block email/domain"** → Click **Go**.

### Tasks

We use [Django Q2](https://django-q2.readthedocs.io/) for scheduling (repeated) tasks.

#### Run the cluster

```bash
python manage.py qcluster
```

#### Monitor

Details: <https://django-q2.readthedocs.io/en/master/monitor.html>

tl;dr:

```bash
python manage.py qmonitorq

python manage.py qinfo
```

### Trigger creation of data export files

TODO

## License

This software is published under the GNU General Public License v3.0 (see file `LICENSE`).
For licenses of used libraries and dependencies, e.g., scripts and CSS files in `publications/static/`, see respective files and projects.
