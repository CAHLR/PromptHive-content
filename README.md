# Getting Started with Create React App

This project was bootstrapped with [Create React App](https://github.com/facebook/create-react-app).

## Latest deployment here: [OATutor Netlify Integration](https://cahlprompthive.netlify.app/#/oatutor-integration)


##  Major Features added
- Ability to edit all spreadsheet content in-place within the app
- User experience improvements: Submit button for URL, autopopulation of content interface 
- Added functionality for question generation: ability to generate variations of existing questions and from a user-inputted Syllabus 
- Abscribe -> PromptHive Rebranding
- Implemented functionality for service accounts
- Logging of LLM Model and Temperature
  
## To-Dos
- Ability to undo the prompt scratchpad (e.g., if you ‘x’ out a prompt from the scratch pad and want to undo that action)
- Fix the prompt library (currently saving prompts doesn't work, most likely related to the mongodb credentials)
- Auto-generation of titles and content based on a syllabus: Upon entering the app, a user should have the option to input a syllabus, and then generate OATutor spreadsheet titles and questions based off the syllabus.
  
## Available Scripts

In the project directory, you can run:

### `npm start`

Runs the app in the development mode.\
Open [http://localhost:3000](http://localhost:3000) to view it in your browser.

If npm opens the app on a different port, and you'd like \
to change it to 3000, use npm start -- --port 3000.

The page will reload when you make changes.\
You may also see any lint errors in the console.

### `npm test`

Launches the test runner in the interactive watch mode.\
See the section about [running tests](https://facebook.github.io/create-react-app/docs/running-tests) for more information.

### `npm run build`

Builds the app for production to the `build` folder.\
It correctly bundles React in production mode and optimizes the build for the best performance.

The build is minified and the filenames include the hashes.\
Your app is ready to be deployed!

See the section about [deployment](https://facebook.github.io/create-react-app/docs/deployment) for more information.

### `npm run eject`

**Note: this is a one-way operation. Once you `eject`, you can't go back!**

If you aren't satisfied with the build tool and configuration choices, you can `eject` at any time. This command will remove the single build dependency from your project.

Instead, it will copy all the configuration files and the transitive dependencies (webpack, Babel, ESLint, etc) right into your project so you have full control over them. All of the commands except `eject` will still work, but they will point to the copied scripts so you can tweak them. At this point you're on your own.

You don't have to ever use `eject`. The curated feature set is suitable for small and middle deployments, and you shouldn't feel obligated to use this feature. However we understand that this tool wouldn't be useful if you couldn't customize it when you are ready for it.
## Setting up the Front-End
Prerequisite: Download [Node.js](https://nodejs.org/en/download)
1. cd to frontend `cd frontend`
2. Install dependencies `npm install`
3. Run the app locally `npm start`
   
## Setting up the Back-End
To run ABScribe, you also need to spin up the back-end database and Flask application. Follow the instructions in
[the backend readme](/abscribe_backend/README.md) to get that running. 

Then, edit [abscribeAPI.js](/frontend/src/services/abscribeAPI.js) and change apiClient.baseURL to be the port you're \
running the Flask app on to connect the two so front-end requests will actually \
make it to the back-end.

Also, edit [config.jsx](frontend/src/config.jsx) and change API_BASE_URL to your running backend URL (eg. http://127.0.0.1:8080) \ 

## Deployment to Production

### Deploying the backend
Run the following commands on your backend machine and use a tool for session persistence such as tmux or GNU Screen
1. Start your virtual environment (refer to [the backend readme](/abscribe_backend/README.md)) 
2. `cd abscribe_backend/abscribe_backend`
3. Replace <your-domain> with your domain name and start your backend server with SSL certificates (Change path of certfile and keyfile if necessary) `gunicorn -k gevent -w 9 -b 0.0.0.0:8081 \
  --certfile=/etc/letsencrypt/live/<your-domain>/fullchain.pem \
  --keyfile=/etc/letsencrypt/live/<your-domain>/privkey.pem \
  abscribe_backend.app:app`

### Deploying the frontend
1. `cd frontend`
2. Update the backend url inside [config.jsx](frontend/src/config.jsx) 
3. run `npm run build` to build the application
4. Use a frontend hosting service such as netlify, vercel, railway, etc. to deploy.

## Learn More

You can learn more in the [Create React App documentation](https://facebook.github.io/create-react-app/docs/getting-started).

To learn React, check out the [React documentation](https://reactjs.org/).

### Code Splitting

This section has moved here: [https://facebook.github.io/create-react-app/docs/code-splitting](https://facebook.github.io/create-react-app/docs/code-splitting)

### Analyzing the Bundle Size

This section has moved here: [https://facebook.github.io/create-react-app/docs/analyzing-the-bundle-size](https://facebook.github.io/create-react-app/docs/analyzing-the-bundle-size)

### Making a Progressive Web App

This section has moved here: [https://facebook.github.io/create-react-app/docs/making-a-progressive-web-app](https://facebook.github.io/create-react-app/docs/making-a-progressive-web-app)

### Advanced Configuration

This section has moved here: [https://facebook.github.io/create-react-app/docs/advanced-configuration](https://facebook.github.io/create-react-app/docs/advanced-configuration)

### Deployment

This section has moved here: [https://facebook.github.io/create-react-app/docs/deployment](https://facebook.github.io/create-react-app/docs/deployment)

### `npm run build` fails to minify

This section has moved here: [https://facebook.github.io/create-react-app/docs/troubleshooting#npm-run-build-fails-to-minify](https://facebook.github.io/create-react-app/docs/troubleshooting#npm-run-build-fails-to-minify)
