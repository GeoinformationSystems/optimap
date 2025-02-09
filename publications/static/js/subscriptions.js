document.addEventListener("DOMContentLoaded", function () {
    function getCSRFToken() {
        let cookieValue = null;
        if (document.cookie && document.cookie !== "") {
            let cookies = document.cookie.split(";");
            for (let i = 0; i < cookies.length; i++) {
                let cookie = cookies[i].trim();
                if (cookie.startsWith("csrftoken=")) {
                    cookieValue = cookie.substring("csrftoken=".length, cookie.length);
                    break;
                }
            }
        }
        return cookieValue;
    }

    async function fetchSubscriptions() {
        try {
            let response = await fetch("/api/subscriptions/");  // ✅ Corrected API URL
            if (!response.ok) throw new Error(`Failed to fetch subscriptions: ${response.status}`);
            let data = await response.json();
            console.log("DEBUG: Subscription Data", data);
            return data;
        } catch (error) {
            console.error("Error fetching subscriptions:", error);
            return { subscriptions: [] };
        }
    }

    async function renderSubscriptions() {
        let data = await fetchSubscriptions();
        let subList = document.getElementById("subscription-list");
        subList.innerHTML = "";

        if (data.subscriptions.length > 0) {
            data.subscriptions.forEach(subscription => {
                let subItem = document.createElement("li");
                subItem.className = "list-group-item d-flex justify-content-between align-items-center";
                subItem.setAttribute("data-sub-id", subscription.id);
                subItem.innerHTML = `${subscription.name} 
                    <button class="btn btn-sm btn-danger delete-subscription-btn" data-sub-id="${subscription.id}">Remove</button>`;
                subList.appendChild(subItem);
            });
        } else {
            subList.innerHTML = "<li class='list-group-item'>No subscriptions found.</li>";
        }
    }

    document.getElementById("subscription-form").addEventListener("submit", function (event) {
        event.preventDefault();
        let name = document.getElementById("subscription-name").value;
        let csrfToken = document.querySelector("input[name=csrfmiddlewaretoken]").value;

        fetch("/api/subscriptions/add/", {  // ✅ Corrected API URL
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken
            },
            body: JSON.stringify({ name: name })
        })
            .then(response => response.json())
            .then(data => {
                console.log("DEBUG: Subscription added", data);
                renderSubscriptions();
            })
            .catch(error => console.error("Error:", error));
    });

    function deleteSubscription(subscriptionId) {
        fetch(`/api/subscriptions/delete/${subscriptionId}/`, {  // ✅ Corrected API URL
            method: "DELETE",
            headers: {
                "X-CSRFToken": getCSRFToken()
            }
        })
            .then(response => response.json())
            .then(data => {
                console.log("DEBUG: Subscription deleted", data);
                document.querySelector(`[data-sub-id='${subscriptionId}']`).remove();
            })
            .catch(error => console.error("Error:", error));
    }

    // ✅ Attach event listener instead of inline onclick
    document.addEventListener("click", function (event) {
        if (event.target.classList.contains("delete-subscription-btn")) {
            let subscriptionId = event.target.getAttribute("data-sub-id");
            deleteSubscription(subscriptionId);
        }
    });

    renderSubscriptions();
});

