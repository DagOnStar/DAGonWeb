from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectMultipleField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

class RoleForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save role")

class UserForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[Optional(), Length(min=8)])
    active = BooleanField("Active", default=True)
    roles = SelectMultipleField("Roles", coerce=int)
    submit = SubmitField("Save user")

class SettingsForm(FlaskForm):
    scratch_dir = StringField("Scratch directory", validators=[DataRequired(), Length(max=1024)])
    submit = SubmitField("Save settings")
