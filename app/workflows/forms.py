from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

class WorkflowForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    description = TextAreaField("Description", validators=[Optional()])
    is_public = BooleanField("Public")
    submit = SubmitField("Save workflow")


class RunForm(FlaskForm):
    """User-editable metadata for an immutable execution record."""

    label = StringField("Label", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save run")
