import time, syslog, json
import smtplib
import RPi.GPIO as gpio

from twisted.internet import task
from twisted.internet import reactor
from twisted.web import server
from twisted.web.static import File
from twisted.web.resource import Resource, IResource
from zope.interface import implements

from twisted.cred import checkers, portal
from twisted.web.guard import HTTPAuthSessionWrapper, DigestCredentialFactory, BasicCredentialFactory

class HttpPasswordRealm(object):
    implements(portal.IRealm)
 
    def __init__(self, myresource):
        self.myresource = myresource
    
    def requestAvatar(self, user, mind, *interfaces):
        if IResource in interfaces:
            return (IResource, self.myresource, lambda: None)
        raise NotImplementedError()

class Door(object):
    last_action = None
    last_action_time = None

    time_open = None
    msg_sent = False

    def __init__(self, doorId, config):
        self.id = doorId
        self.name = config['name']
        self.relay_pin = config['relay_pin']
        self.state_pin = config['state_pin']
        self.time_to_close = config.get('time_to_close', 10)
        self.time_to_open = config.get('time_to_open', 10)
        gpio.setup(self.relay_pin, gpio.OUT)
        gpio.setup(self.state_pin, gpio.IN, pull_up_down=gpio.PUD_UP)        
        gpio.output(self.relay_pin, True)
        
    def get_state(self):
        if gpio.input(self.state_pin) == 0:
            return 'closed'
        elif self.last_action == 'open':
            if time.time() - self.last_action_time >= self.time_to_open:
                return 'open'
            else:
                return 'opening'
        elif self.last_action ==  'close':
            if time.time() - self.last_action_time >= self.time_to_close:
                return 'open' # This state indicates a problem
            else:
                return 'closing'
        else:
            return 'open'
        
    def toggle_relay(self):
        state = self.get_state()
        if (state == 'open'):
            self.last_action = 'close'
            self.send_closedoor_message()
            self.last_action_time = time.time()
        elif state == 'closed':
            self.last_action = 'open'
            self.send_opendoor_message()
            self.last_action_time = time.time()
        else:
            self.last_action = None
            self.last_action_time = None
        
        gpio.output(self.relay_pin, False)
        time.sleep(0.2)
        gpio.output(self.relay_pin, True)
        
    def send_opendoor_message(self):
        syslog.syslog("Sending open door message.")
        config = self.config['smtp']
        server = smtplib.SMTP(config["smtphost"], config["smtpport"])
        if (config["smtp_tls"] == "True") :
            server.starttls()
        server.login(config["username"], config["password"])
        message = "Your garage door %s have been opened." % self.name
        server.sendmail(config["username"], config["to_email"], message)
        server.close()
    
     def send_closedoor_message(self):
        syslog.syslog("Sending close door message.")
        config = self.config['smtp']
        server = smtplib.SMTP(config["smtphost"], config["smtpport"])
        if (config["smtp_tls"] == "True") :
            server.starttls()
        server.login(config["username"], config["password"])
        message = "Your garage door %s have been closed." % self.name
        server.sendmail(config["username"], config["to_email"], message)
        server.close() 

class Controller():
    def __init__(self, config):
        gpio.setwarnings(False)
        gpio.cleanup()
        gpio.setmode(gpio.BCM)
        self.open_time = time.time()
        self.msg_sent = False
        self.config = config
        self.doors = [Door(n,c) for (n,c) in config['doors'].items()]
        self.updateHandler = UpdateHandler(self)
        for door in self.doors:
            door.last_state = 'unknown'
            door.last_state_time = time.time()
        
        self.use_smtp = False
        smtp_params = ("smtphost", "smtpport", "smtp_tls", "username", 
                       "password", "to_email", "time_to_wait")
        self.use_smtp = ('smtp' in config) and set(smtp_params) == set(config['smtp'])
        syslog.syslog("Are we using SMTP: %s" % self.use_smtp)        
            
    def status_check(self):
        open_doors = False

        for door in self.doors:
            new_state = door.get_state()
            if (door.last_state != new_state):
                syslog.syslog('%s: %s => %s' % (door.name, door.last_state, new_state))
                door.last_state = new_state
                door.last_state_time = time.time()
                self.updateHandler.handle_updates()
            if not new_state == 'closed':
                open_doors = True
                
        if self.use_smtp:
            ttw = self.config['smtp']["time_to_wait"]
            if open_doors and not self.msg_sent and time.time() - self.open_time >= ttw:
                self.send_opendoor_message(int(time.time() - self.open_time))
        
        if not open_doors:
            self.open_time = time.time()
            self.msg_sent = False
                
    def send_opendoor_message(self, opentime):
        syslog.syslog("Sending open door message. (%s)" % opentime)
        config = self.config['smtp']
        server = smtplib.SMTP(config["smtphost"], config["smtpport"])
        if (config["smtp_tls"] == "True") :
            server.starttls()
        server.login(config["username"], config["password"])
        message = "Your garage doors have been open for %s." % elapsed_time(100+opentime)
        server.sendmail(config["username"], config["to_email"], message)
        server.close()
        self.msg_sent = True    
        

    def toggle(self, doorId):
        for d in self.doors:
            if d.id == doorId:
                syslog.syslog('%s: toggled' % d.name)
                d.toggle_relay()
                return
        
    def get_updates(self, lastupdate):
        updates = []
        for d in self.doors:
            if d.last_state_time >= lastupdate:
                updates.append((d.id, d.last_state, d.last_state_time))
        return updates

    def run(self):
        task.LoopingCall(self.status_check).start(0.5)
        root = File('www')
        root.putChild('upd', self.updateHandler)
        root.putChild('cfg', ConfigHandler(self))
        
        clk = ClickHandler(self)
        args={self.config['site']['username']:self.config['site']['password']}
        checker = checkers.InMemoryUsernamePasswordDatabaseDontUse(**args)
        realm = HttpPasswordRealm(clk)
        p = portal.Portal(realm, [checker])
        credentialFactory = BasicCredentialFactory("Garage Door Controller")
        protected_resource = HTTPAuthSessionWrapper(p, [credentialFactory])
        root.putChild('clk', protected_resource)

        site = server.Site(root)
        reactor.listenTCP(self.config['site']['port'], site)  # @UndefinedVariable
        reactor.run()  # @UndefinedVariable

class ClickHandler(Resource):
    isLeaf = True
    
    def __init__ (self, controller):
        Resource.__init__(self)
        self.controller = controller
    
    def render(self, request):
        door = request.args['id'][0]
        self.controller.toggle(door)
        return 'OK'

class ConfigHandler(Resource):
    isLeaf = True
    def __init__ (self, controller):
        Resource.__init__(self)
        self.controller = controller
    
    def render(self, request):
        request.setHeader('Content-Type', 'application/json')
        
        return json.dumps([(d.id, d.name, d.last_state, d.last_state_time)
                            for d in controller.doors])         
        

class UpdateHandler(Resource):
    isLeaf = True
    def __init__(self, controller):
        Resource.__init__(self)
        self.delayed_requests = []
        self.controller = controller
    
    def handle_updates(self):
        for request in self.delayed_requests:
            updates = self.controller.get_updates(request.lastupdate)
            if updates != []:
                self.send_updates(request, updates)
                self.delayed_requests.remove(request);
    
    def format_updates(self, request, update):
        response = json.dumps({'timestamp': int(time.time()), 'update':update})
        if hasattr(request, 'jsonpcallback'):
            return request.jsonpcallback +'('+response+')'
        else:
            return response
            
    def send_updates(self, request, updates):
        request.write(self.format_updates(request, updates))
        request.finish()
    
    def render(self, request):
        
        # set the request content type
        request.setHeader('Content-Type', 'application/json')
        
        # set args
        args = request.args
       
        # set jsonp callback handler name if it exists
        if 'callback' in args:
            request.jsonpcallback =  args['callback'][0]
           
        # set lastupdate if it exists
        if 'lastupdate' in args:
            request.lastupdate = float(args['lastupdate'][0])
        else:
            request.lastupdate = 0
            
            #print "request received " + str(request.lastupdate)    
            
        # Can we accommodate this request now?
        updates = controller.get_updates(request.lastupdate)
        if updates != []:
            return self.format_updates(request, updates)
        
        
        request.notifyFinish().addErrback(lambda x: self.delayed_requests.remove(request))
        self.delayed_requests.append(request)
        
        # tell the client we're not done yet
        return server.NOT_DONE_YET

def elapsed_time(seconds, suffixes=['y','w','d','h','m','s'], add_s=False, separator=' '):
    """
    Takes an amount of seconds and turns it into a human-readable amount of time.
    """
    # the formatted time string to be returned
    time = []
    
    # the pieces of time to iterate over (days, hours, minutes, etc)
    # - the first piece in each tuple is the suffix (d, h, w)
    # - the second piece is the length in seconds (a day is 60s * 60m * 24h)
    parts = [(suffixes[0], 60 * 60 * 24 * 7 * 52),
             (suffixes[1], 60 * 60 * 24 * 7),
             (suffixes[2], 60 * 60 * 24),
             (suffixes[3], 60 * 60),
             (suffixes[4], 60),
             (suffixes[5], 1)]
    
    # for each time piece, grab the value and remaining seconds, and add it to
    # the time string
    for suffix, length in parts:
        value = seconds / length
        if value > 0:
            seconds = seconds % length
            time.append('%s%s' % (str(value),
                                  (suffix, (suffix, suffix + 's')[value > 1])[add_s]))
        if seconds < 1:
            break

    return separator.join(time)
     
if __name__ == '__main__':
    syslog.openlog('garage_controller')
    config_file = open('config.json')
    controller = Controller(json.load(config_file))
    config_file.close()
    controller.run()
              
    
