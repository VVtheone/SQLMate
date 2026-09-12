class LoginGui:
    def __init__(self):
        self.correct_password=False
        import tkinter as tk
        from tkinter import messagebox
        import time

        # Dictionary storing usernames as keys and passwords as values
        username_data = {
            "admin": "admin123",
            "john": "john456",
            "alice": "alicepass"
        }


        def check_credentials():
            # Save entered username and password into variables
            entered_username = username_entry.get()
            entered_password = password_entry.get()

            # Compare against the dictionary
            if entered_username in username_data and username_data[entered_username] == entered_password:
                print("correct")
                result_label.config(text="Correct", fg="green")
                self.correct_password=True
                root.destroy()





            else:
                print("incorrect")
                result_label.config(text="Incorrect", fg="red")
                messagebox.showerror("Login", "Incorrect")


        # Set up the main window
        root = tk.Tk()
        root.title("Login")
        root.geometry("300x200")

        # Username field
        tk.Label(root, text="Username:").pack(pady=(20, 0))
        username_entry = tk.Entry(root)
        username_entry.pack()

        # Password field (masked with *)
        tk.Label(root, text="Password:").pack(pady=(10, 0))
        password_entry = tk.Entry(root, show="*")
        password_entry.pack()

        # Check button
        check_button = tk.Button(root, text="Check", command=check_credentials)
        check_button.pack(pady=15)

        # Result label
        result_label = tk.Label(root, text="", font=("Arial", 10, "bold"))
        result_label.pack()

        root.mainloop()
