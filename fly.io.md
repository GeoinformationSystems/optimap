# Fly.io

## Prerequisites

Install `flyctl`:

```bash
curl -L https://fly.io/install.sh | sh
```

## Create PostGIS

- <https://community.fly.io/t/deploying-postgis/3530>
- <https://fly.io/docs/reference/postgres-on-nomad/>

```bash
flyctl postgres create
# follow the instructions, use name optimap-db, region Frankfurt or Amsterdam, configuration "Development", do not scale to 0

flyctl image show --app optimap-db

# PostGIS needs a bit more memory; THIS IS NOT FREE: https://fly.io/docs/about/pricing/?utm_campaign=oom-notification&utm_medium=email&utm_source=flyio#virtual-machines
fly status --app optimap-db
fly machine update [see output from previous command] --memory 512 --app optimap-db
```

Note the username, password, connection string etc. in a secure place and manually set `DATABASE_URL` for the app.
Note that you need to change connection type to `postgis`, i.e., start with `postgis://`.

Create database and enable PostGIS

```bash
flyctl postgres connect -a optimap-db

# in postgres=# 
CREATE DATABASE optimap;

\connect optimap

CREATE EXTENSION postgis;
```

If you want to start from scratch with an empty database, you can use

```bash
# CAREFUL!
#DROP DATABASE optimap WITH (FORCE);
```

Using the client connection above or connect with pgAdmin4 and do the same with the UI.

## Deploy app via Dockerfile

Use Dockerfile instead of [Django on Fly](https://fly.io/docs/django/) because of GIS dependencies.
See also the following useful tutorials:

- <https://learndjango.com/tutorials/deploy-django-postgresql-flyio>
- <https://fly.io/docs/languages-and-frameworks/dockerfile/>
- <https://dev.to/teachmetechy/django-rest-framework-on-flyio-582p>

_Note:_ See the file `fly.toml` for a number of environment variables and configurations that are already set!

From the interactive UI we learned:

> We recommend using the database_url (`pip install dj-database-url`) to parse the DATABASE_URL from os.environ['DATABASE_URL']
>
> For detailed documentation, see <https://fly.dev/docs/django/>

The current configuration style uses `dj-database-uri`.
However, because the database connection is not available when the migrations are run, this does not work rightaway.
We cannot set database connection because the app does not exist at this point.

Therefore, now set the `DATABASE_URL` before deploying.

```bash
flyctl secret set DATABASE_URL=DATABASE_URL=postgis://...
```

Check the secret exists:

```bash
flyctl secrets list
```

Launch the app using the Dockerfile:

```bash
# only used for the first configuration:
flyctl launch --dockerfile Dockerfile

flyctl deploy
# on first run, overwrite Dockerfile and then roll back needed changes, otherwise error
```

In the interactive UI of `launch`:

- copy configuration: yes
- name 'optimap'
- create a Postgres DB: yes
- connect to `optimap-db`: yes
- existing database, continue attachment: yes
- (ignore the shown secret for the database connection to connect as the app user, because the migrations need the postgres user)

## Get IPs and certificate

- <https://fly.io/docs/flyctl/ips/#usage>
- <https://fly.io/docs/app-guides/custom-domains-with-fly/>

```bash
#fly ips allocate-v4 # paid feature!
fly ips allocate-v6
```

```bash
fly ips list
```

Configure `A` and `AAAA` records for `@` and `www` (e.g., with IONOS) at domain registrar using the information of the previous command's output.
Then continue with

```bash
flyctl certs create optimap.science
#flyctl certs create www.optimap.science
```

Check IP config:

```bash
traceroute optimap.fly.dev
traceroute optimap.science
```

This should all looks good!

## Secrets and passwords

Check that `SECRET_KEY` environment variable is set, otherwise set it to something... secret:

```bash
flyctl secrets list
flyctl secrets set SECRET_KEY="..."
```

Configure **login email** password (other values are set in `fly.toml`):

```bash
flyctl secrets set OPTIMAP_EMAIL_HOST_PASSWORD="..."
```

Configure the **superusers' email** (users registering with this emailaddress will become Django superusers), seperated with `,` (comma):

```bash
flyctl secrets set OPTIMAP_SUPERUSER_EMAILS="email@server,email@server,..."
```

You can also set values for the secrets in the Fly.io dashboard.

## Deploy

Using the previously configured settings and configurations:

```bash
flyctl deploy
```

Then open <https://optimap.fly.dev/> and <https://optimap.science/>.

## Update allowed hosts and configure CSRF

- <https://learndjango.com/tutorials/deploy-django-postgresql-flyio>
- See <https://github.com/ifgi/optimetaPortal/issues/42> for links and issue description around CSRF

Add to `fly.toml`:

```toml
  CSRF_TRUSTED_ORIGINS = "https://optimap.science"
```

Then `flyctl deploy`.

## Connect to database locally

```bash
fly proxy 15432:5432 -a optimap-db
```

Connect to database locally at port `15432`, e.g., with pgAdmin.

## Get a shell in the app container

```bash
flyctl --app optimap console
```

Inspect the settings,  load test data, etc.

```bash
python manage.py loaddata fixtures/test_data.json

python manage.py diffsettings
```

## Disable the app

For example, when you want to manipulate the database without any open connections:

```bash
flyctl scale count 0
flyctl status
```

To re-enable, set the scale count to `1`.

## Future

- Database backups, see <https://www.joseferben.com/posts/django-on-flyio/>
- Health check endpoint, see <https://www.joseferben.com/posts/django-on-flyio/>
