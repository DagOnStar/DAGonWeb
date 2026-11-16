from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectMultipleField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional, Regexp
from wtforms.widgets import CheckboxInput, ListWidget


class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


class RoleForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80), Regexp(r"^[a-z0-9_-]+$", message="Use lower-case letters, digits, - and _.")])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    permissions = MultiCheckboxField("Permissions")
    submit = SubmitField("Save role")


class UserForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=255)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[Optional(), Length(min=8)])
    active = BooleanField("Active", default=True)
    roles = MultiCheckboxField("Roles", coerce=int)
    unlink_sso = BooleanField("Unlink single sign-on identity")
    submit = SubmitField("Save user")


class DagonIniForm(FlaskForm):
    content = TextAreaField("dagon.ini", validators=[DataRequired(), Length(max=65536)])
    submit = SubmitField("Save dagon.ini")
