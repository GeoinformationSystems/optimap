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

```bash
docker-compose up

# run migrations, in the directory where docker-compose is to resolve the name "web"
docker-compose run web python manage.py makemigrations
docker-compose run web python manage.py migrate
```

Now open a browser at <http://localhost:8000/>.

## Development

### Test data

The folder `/fixtures` contains some test data, either as an SQL command to insert into the database, or as a database dump that was created and can be loaded with [`django-admin`](https://docs.djangoproject.com/en/dev/ref/django-admin/).
[`jq`](https://stedolan.github.io/jq/) is used for pretty-printing of the output.

```bash
# create dump after creating/harvesting test data:
python manage.py dumpdata --exclude=auth --exclude=contenttypes | jq > fixtures/test_data.json

# load:
python manage.py loaddata fixtures/test_data.json
```

If you want to create more testdata manually, copy and paste the existing records or add more records in the admin backend (preferred).
A useful tool for creating sensible geometries is <https://wktmap.com/>.

### Run locally

1. Create a `.env` file based on `.env.example` in the same directory where `settings.py` resides and fill in the configuration settings as needed.
2. Run the commands below, which use the built-in Python [`venv`](https://docs.python.org/3/library/venv.html), see [this tutorial](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/#create-and-use-virtual-environments) for details

```bash
# once only: create virtual environment
# python -m venv .venv
source .venv/bin/activate
which python

pip install -r requirements.txt

# create local DB container (once)
# docker run --name optimapDB -p 5432:5432 -e POSTGRES_USER=optimap -e POSTGRES_PASSWORD=optimap -e POSTGRES_DB=optimap -d postgis/postgis:14-3.3

# start DB
docker start optimapDB

# run migrations
python manage.py makemigrations
python manage.py migrate

# create cache table
python manage.py createcachetable

# collect static files
python manage.py collectstatic --noinput

# start app
python manage.py runserver

# start app with configuration for development
OPTIMAP_CACHE=dummy OPTIMAP_DEBUG=True python manage.py runserver
```

Now open a browser at <http://127.0.0.1:8000/>.

See instructions below for creating a superuser and above for loading test data.

When you are done, deactivate the virtual environment and stop the DB with:

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

### Create superusers/admin

Superusers/admin can be created using the createsuperuser command:

```bash
python manage.py createsuperuser --username=optimap --email=nomail@optimap.science
```

You will be prompted for a password.
After you enter one, the user will be created immediately. If you leave off the --username or --email options, it will prompt you for those values.

You can acess the admin page at <http://127.0.0.1:8000/admin/>.

You can also run the command in a containerised app with `docker-compose run web python manage.py ...`.

## Block Emails/Domains

### What It Does

- Blocks specific emails and entire domains from registering.
- Prevents login attempts from blocked users.
- Admin can delete users and instantly block their email/domain.

### How to Use in Django Admin

1. **Manually Add Blocked Emails/Domains**
   - Go to `/admin/`
   - Add emails in **Blocked Emails** or domains in **Blocked Domains**.
2. **Block Users via Admin Action**
   - Go to `/admin/auth/user/`
   - Select users → Choose **"Delete user and block email/domain"** → Click **Go**.

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
