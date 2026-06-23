from app import create_app, seed_defaults
from app.config import TestConfig
from app.extensions import db
from app.models import Role, User, WorkflowLink


def make_app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        seed_defaults(app)
    return app


def test_seed_creates_admin_and_roles():
    app = make_app()
    with app.app_context():
        assert Role.query.filter_by(name="admin").first() is not None
        assert Role.query.filter_by(name="user").first() is not None
        assert User.query.filter_by(email="admin@example.org").first() is not None


def test_workflow_link_uri_contract():
    link = WorkflowLink(source_uid="a", source_output="output", target_uid="b", target_input="input", workflow_uri="workflow://a/output")
    assert link.to_dict()["workflow_uri"] == "workflow://a/output"
