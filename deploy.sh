sudo --validate
git pull
uv run manage.py migrate
uv run manage.py collectstatic --noinput
sudo systemctl restart huey_expenses.service
sudo systemctl restart gunicorn_expenses.service