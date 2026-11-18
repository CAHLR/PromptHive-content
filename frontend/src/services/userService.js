import apiClient from "./abscribeAPI";
import data from "bootstrap/js/src/dom/data";
export let userService = {
    deactivateUser(email) {
        apiClient.delete(`/user/${email}`);
    },
    createUser(email, unsecure_password, roles) {
      apiClient.post("/user", {
          "email": email,
          "password": unsecure_password, // This will be hashed and made secure in the back-end. Use HTTPS! USE HTTPS!! Or this will be visible.
          "roles": roles
      })
    },
    loginUser(email, unsecure_password) {
        apiClient.post("/login", {
            "email": email,
            "password": unsecure_password
        })
    },
    logoutCurrentUser(){
        apiClient.post("/logout")
    }

}
