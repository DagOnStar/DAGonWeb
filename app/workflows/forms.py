from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional, Regexp

DAGONSTAR_NAME_RE = r"^[A-Za-z0-9_-]+$"
DAGONSTAR_NAME_MESSAGE = "Use only letters, numbers, underscores, and hyphens."

class WorkflowForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80), Regexp(DAGONSTAR_NAME_RE, message=DAGONSTAR_NAME_MESSAGE)])
    description = TextAreaField("Description", validators=[Optional()])
    is_public = BooleanField("Public")
    submit = SubmitField("Save workflow")


class RunForm(FlaskForm):
    """User-editable metadata for an immutable execution record."""

    label = StringField("Label", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save run")
