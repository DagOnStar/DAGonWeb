from flask_wtf import FlaskForm
from wtforms import BooleanField, HiddenField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember me")
    submit = SubmitField("Sign in")

class RegisterForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)], render_kw={"autocomplete": "email"})
    submit = SubmitField("Send verification link")


class CompleteRegistrationForm(FlaskForm):
    token = HiddenField(validators=[DataRequired()])
    name = StringField("Display name", validators=[Optional(), Length(max=255)], render_kw={"autocomplete": "name"})
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10)], render_kw={"autocomplete": "new-password"})
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", message="The passwords do not match.")], render_kw={"autocomplete": "new-password"})
    accept_privacy = BooleanField("I have read the privacy notice", validators=[DataRequired(message="Please read and accept the privacy notice to create an account.")])
    submit = SubmitField("Create account")
