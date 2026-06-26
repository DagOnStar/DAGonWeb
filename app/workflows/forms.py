from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, Regexp

DAGONSTAR_NAME_RE = r"^[A-Za-z0-9_-]+$"
DAGONSTAR_NAME_MESSAGE = "Use only letters, numbers, underscores, and hyphens."

class WorkflowForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80), Regexp(DAGONSTAR_NAME_RE, message=DAGONSTAR_NAME_MESSAGE)])
    description = TextAreaField("Description", validators=[Optional()])
    is_public = BooleanField("Public")
    submit = SubmitField("Save workflow")


class WorkflowSetupForm(WorkflowForm):
    batch_threads = IntegerField("Batch threads", validators=[Optional(), NumberRange(min=1, max=256)], default=1)
    dagon_service_route = StringField("DAGon service route", validators=[Optional(), Length(max=255)])
    dagon_service_use = BooleanField("Use DAGon service")
    ftp_pub_ip = StringField("FTP public host", validators=[Optional(), Length(max=255)])
    fair_enabled = BooleanField("Record FAIR provenance for runs")
    fair_title = StringField("FAIR title", validators=[Optional(), Length(max=255)])
    fair_creators = TextAreaField("Creators", validators=[Optional(), Length(max=2000)])
    fair_license = StringField("License", validators=[Optional(), Length(max=255)])
    fair_keywords = StringField("Keywords", validators=[Optional(), Length(max=1000)])
    fair_access_policy = StringField("Access policy", validators=[Optional(), Length(max=255)])
    fair_strict = BooleanField("Strict FAIR validation")
    fair_capture_environment = BooleanField("Capture allowlisted environment")
    fair_environment_allowlist = StringField("Environment allowlist", validators=[Optional(), Length(max=1000)])
    dagon_ini_content = TextAreaField("Advanced dagon.ini", validators=[Optional(), Length(max=65536)])
    submit = SubmitField("Save workflow setup")


class RunForm(FlaskForm):
    """User-editable metadata for an immutable execution record."""

    label = StringField("Label", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save run")
