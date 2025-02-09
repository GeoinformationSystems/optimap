//to trigger datepicker 
$(function () {
    $('#datepicker1').datepicker({
        clearBtn : true,              
    });
    $('#datepicker2').datepicker({
        clearBtn : true,              
    });
});

async function getsubscription() {
    const subscription_url = '/api/subscriptions/'
    try {
        let res = await fetch(subscription_url);
        return await res.json();
    } catch (error) {
        console.log(error);
    }
}

async function rendersubscriptions() {
    let data = await getsubscription();
    const f = data.features.length
    if (f >= 1) {
        for (var index = 0; index < f ; ++index){
            temp = document.getElementById("accordion1");
            let p = document.createElement("div")
            p.className = "card";
            temp.appendChild(p);
            let q = document.createElement("div");
            q.className = "card-header";
            q.id = "heading";
            count_heading = index+1 ;
            q.id += count_heading;
            p.appendChild(q);
            let btn = document.createElement("button");
            btn.className = "btn btn-link";
            btn.setAttribute("data-toggle","collapse");
            c = "collapse"
            count_collapse = index+1 ;
            c += count_collapse;
            btn.setAttribute("data-target","#"+c);
            btn.setAttribute("aria-expanded","false");
            btn.setAttribute("aria-controls",c);
            btn.innerHTML = data.features[index].properties['search_text'];
            q.appendChild(btn);
            let r = document.createElement("div");
            r.id = c;
            r.className = "collapse";
            r.setAttribute("aria-labelledby",q.id);
            r.setAttribute("data-parent","#accordion1");
            p.appendChild(r); 
            let s = document.createElement("div");
            s.className = "card-body";
            s.innerHTML = 'timeperiod : ' + data.features[index].properties['timeperiod_startdate'] + "//" + data.features[index].properties['timeperiod_enddate'] ;
            r.appendChild(s);                   
        }   
    } else { 
        temp = document.getElementById("accordion1");
        temp.innerHTML += '<br>'+'<p class = "lead">'+ "You do not have any active subscriptions yet. Please click on Add New Subcription to add journals you want!" + '</p>'
    }
}
rendersubscriptions();


//to trigger datepicker 
$(function () {
    $('#datepicker1').datepicker({
        clearBtn : true,              
    });
    $('#datepicker2').datepicker({
        clearBtn : true,              
    });
});

async function getsubscription() {
    const subscription_url = '/api/subscriptions/'
    try {
        let res = await fetch(subscription_url);
        return await res.json();
    } catch (error) {
        console.log(error);
    }
}

async function rendersubscriptions() {
    let data = await getsubscription();
    const f = data.features.length
    if (f >= 1) {
        for (var index = 0; index < f ; ++index){
            temp = document.getElementById("accordion1");
            let p = document.createElement("div")
            p.className = "card";
            temp.appendChild(p);
            let q = document.createElement("div");
            q.className = "card-header";
            q.id = "heading";
            count_heading = index+1 ;
            q.id += count_heading;
            p.appendChild(q);
            let btn = document.createElement("button");
            btn.className = "btn btn-link";
            btn.setAttribute("data-toggle","collapse");
            c = "collapse"
            count_collapse = index+1 ;
            c += count_collapse;
            btn.setAttribute("data-target","#"+c);
            btn.setAttribute("aria-expanded","false");
            btn.setAttribute("aria-controls",c);
            btn.innerHTML = data.features[index].properties['search_text'];
            q.appendChild(btn);
            let r = document.createElement("div");
            r.id = c;
            r.className = "collapse";
            r.setAttribute("aria-labelledby",q.id);
            r.setAttribute("data-parent","#accordion1");
            p.appendChild(r); 
            let s = document.createElement("div");
            s.className = "card-body";
            s.innerHTML = 'timeperiod : ' + data.features[index].properties['timeperiod_startdate'] + "//" + data.features[index].properties['timeperiod_enddate'] ;
            r.appendChild(s);                   
        }   
    } else { 
        temp = document.getElementById("accordion1");
        temp.innerHTML += '<br>'+'<p class = "lead">'+ "You do not have any active subscriptions yet. Please click on Add New Subcription to add journals you want!" + '</p>'
    }
}
rendersubscriptions();





