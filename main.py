from login_gui import LoginGui
import gui_app

login_session = LoginGui()

if login_session.correct_password:
    gui_app.launch_app()
    from gui_app import user_input
else:
    print("Login failed. Exiting without opening the app.")


print(user_input)
