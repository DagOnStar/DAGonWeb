from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, PasswordField, SelectMultipleField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional

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
    smtp_host = StringField("SMTP host", validators=[Optional(), Length(max=255)])
    smtp_port = IntegerField("SMTP port", validators=[Optional(), NumberRange(min=1, max=65535)], default=587)
    smtp_from = StringField("From address", validators=[Optional(), Length(max=255)])
    smtp_user = StringField("SMTP username", validators=[Optional(), Length(max=255)])
    smtp_password = PasswordField("SMTP password", validators=[Optional(), Length(max=255)])
    smtp_tls = BooleanField("Use STARTTLS", default=True)
    submit = SubmitField("Save settings")


class DagonIniForm(FlaskForm):
    content = TextAreaField("dagon.ini", validators=[DataRequired(), Length(max=65536)])
    submit = SubmitField("Save dagon.ini")
