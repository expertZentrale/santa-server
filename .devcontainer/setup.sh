#!/bin/bash
set -e

git config --global --add safe.directory "$(pwd)"

echo "Waiting for SQL Server and creating the ${DB_NAME} database..."
python scripts/create_database.py

# SQL Server refuses logins for a few seconds after its first start
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    python manage.py migrate --noinput && break
    echo "migrate failed, retrying in 5s ($attempt/10)"
    sleep 5
done
python manage.py collectstatic --noinput

echo "Setup complete!"
echo 'Create a user with "python manage.py createsuperuser"'
echo 'Then run "python manage.py runserver 0.0.0.0:8000" and open http://localhost:8000/'
echo 'Run the tests with "python manage.py test"'
