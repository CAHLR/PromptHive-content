/** A service used to stream ChatGPT responses from the Flask app using Server Sent Events. (SSEs let us
 * receive messages from the server without overtly making a request for them). **/

// TODO: Make this work with route variables. Currently a placeholder.
const messageEventSource = new EventSource("localhost:8080/message/", {});

// Set up the EventSource to receive messages from the server.
messageEventSource.onmessage = (event) => {
    console.log(`I received the message: ${event.data}`);
}
