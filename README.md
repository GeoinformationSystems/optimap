[![OPTIMETA Logo](https://projects.tib.eu/fileadmin/_processed_/e/8/csm_Optimeta_Logo_web_98c26141b1.png)](https://projects.tib.eu/optimeta/en/) [![KOMET Logo](https://projects.tib.eu/fileadmin/templates/komet/tib_projects_komet_1150.png)](https://projects.tib.eu/komet/en/)

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

## Configuration

All configuration is done via the file `optimap/settings.py`.
Configurations that need to be changed for different installations and for deployment are also exposed as environment variables.
The names of these environment variables start with `OPTIMAP_`.
The settings files loads these from a file `.env` stored in the same location as `settings.py`, or from the environment the server is run it.
A complete list of existing parameters is provided in the file `optimap/.env.example`.

## Run with Docker

The project is containerized using Docker, with services defined in `docker-compose.yml`. To start all services, run:

```bash
docker-compose up
```

### Initial Setup

After starting the containers, apply database migrations:

```bash
# run migrations, in the directory where docker-compose is to resolve the name "web"
docker-compose run web python manage.py makemigrations
docker-compose run web python manage.py migrate
```

Now open a browser at <http://localhost:8001/>.

#### Services Overview

- db: Runs a PostgreSQL database with PostGIS extensions. Data is persisted in a Docker volume named db_data.
- app: Python-based app serving HTTP requests.
- web: Our primary Django web application.
- webserver: An Nginx server for serving static files and test files.

#### Ports

- 5434: Database (PostgreSQL/PostGIS)
- 8002: App (Python HTTP server)
- 8001: Web application (Django server)
- 8080: Webserver (Nginx)

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

# Install required dependencies
pip install -r requirements.txt

# Create a local database container (once only)
docker run --name optimapDB -p 5432:5432 -e POSTGRES_USER=optimap -e POSTGRES_PASSWORD=optimap -e POSTGRES_DB=optimap -d postgis/postgis:15-3.4

# Start the database container
docker start optimapDB

# Apply database migrations
python manage.py makemigrations
python manage.py migrate

# Create a cache table
python manage.py createcachetable

# Collect static files
python manage.py collectstatic --noinput

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

### Debug with VS Code

Select the Python interpreter created above (`optimap` environment), see instructions at <https://code.visualstudio.com/docs/python/tutorial-django> and <https://code.visualstudio.com/docs/python/environments>.

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

Access the admin interface at http://127.0.0.1:8000/admin/.

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

## Deploy

Deploy using docker-compose or see [`fly.io.md`](fly.io.md) for notes on deploying to Fly.io.

## License

This software is published under the GNU General Public License v3.0 (see file `LICENSE`).
For licenses of used libraries and dependencies, e.g., scripts and CSS files in `publications/static/`, see respective files and projects.
