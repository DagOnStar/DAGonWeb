from __future__ import annotations

import os
from flask import Flask, redirect, url_for
from flask_login import current_user
from .config import Config
from .extensions import db, migrate, login_manager, csrf
from .models import Role, Setting, User


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["SCRATCH_DIR"], exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "auth.login"

    from .auth.routes import bp as auth_bp
    from .admin.routes import bp as admin_bp
    from .workflows.routes import bp as workflows_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(workflows_bp, url_prefix="/workflows")

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        return db.session.get(User, int(user_id))

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("workflows.list_workflows"))
        return redirect(url_for("auth.login"))

    @app.cli.command("seed")
    def seed() -> None:
        seed_defaults(app)
        print("Seeded DAGonWeb defaults")

    return app


def seed_defaults(app: Flask) -> None:
    admin = Role.query.filter_by(name="admin").first() or Role(name="admin", description="Administrators")
    user_role = Role.query.filter_by(name="user").first() or Role(name="user", description="Standard users")
    db.session.add_all([admin, user_role])
    db.session.flush()
    email = os.getenv("ADMIN_EMAIL", "admin@example.org")
    password = os.getenv("ADMIN_PASSWORD", "admin12345")
    name = os.getenv("ADMIN_NAME", "DAGonWeb Admin")
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name, active=True, roles=[admin, user_role])
        user.set_password(password)
        db.session.add(user)
    if not Setting.query.filter_by(key="scratch_dir").first():
        db.session.add(Setting(key="scratch_dir", value=app.config["SCRATCH_DIR"]))
    db.session.commit()
