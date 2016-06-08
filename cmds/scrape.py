import re,sys,unicodedata,time,math,httplib,urllib,random
import config

from functools import wraps
from interface import Interface
from symbol import parameters

import stomper
import socket
from twisted.logger import Logger

class StompSocketBuffer(stomper.stompbuffer.StompBuffer): 
    """
    Thread Safe Stomp Buffer
    """  
    def __init__ ( self ):
        self.buffer = ''
        self.lock = threading.Lock()
        
    def appendData ( self, data ):
        self.lock.acquire()
        try:
            self.buffer += data
        finally:
            self.lock.release() 
            
    def getOneMessage ( self ):
        msg = None
        self.lock.acquire()
        try:
            msg = self._getOneMessage()
        finally:
            self.lock.release() 
            return msg
                             
    def _getOneMessage ( self ):
        ( mbytes, hbytes ) = self._findMessageBytes ( self.buffer )
        if not mbytes:
            return None
        
        msgdata = self.buffer[:mbytes]
        self.buffer = self.buffer[mbytes:]
        hdata = msgdata[:hbytes]
        elems = hdata.split ( '\n' )
        cmd     = elems.pop ( 0 )
        headers = {}
        for e in elems:
            try:
                i = e.find ( ':' )
            except ValueError:
                continue
            k = e[:i].strip()
            v = e[i+1:].strip()
            headers [ k ] = v

        body = msgdata[hbytes+2:-2]
        msg = { 'cmd'     : cmd,
                'headers' : headers,
                'body'    : body,
                }
        return msg
                      
class StompSocket(stomper.Engine): 
    def __init__(self, log=None, destination="/queue/derp", server='127.0.0.1', port=61613, username='', password='', buffer=None):       
        
        self.log = self.logz if log == None else log
        self.stompBuffer = buffer
        self.sessionId = ''
        self.states = {
            'CONNECTED' : self.connected,
            'MESSAGE' : self.ack,
            'ERROR' : self.error,
            'RECEIPT' : self.receipt,
        }
        self.state={
            'starting':False,
            'connected':False,
            'subscribed':False,
            'receiving':False,
            'terminating':False
                    }
        
        self.destination = destination
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)         
            self.sock.connect((self.server, self.port))
        except Exception as e:
            self.log("Error connecting to Stomp: %s"%(str(e)))
        
        self.inputs = [self.sock]
        self.outputs = []
        self.message_queues = {}
        self.message_queues['out'] = Queue.Queue()
        self.connect()       
        self.log("StompSocket Created: %s %s"%(self.server, self.destination))
                
    def logz(self,msg):
        print (msg)

    def stop(self):
        self.log("Stomp Socket Closing and Stopping")
        self.state['terminating'] = True
            
    def start(self):
        self.log("Stomp Socket is Starting")
        self.state['starting'] = True
        self.listen()
            
    def listen(self):
        while self.inputs and not self.state['terminating']:
            readable, writable, exceptional = select.select(self.inputs, self.outputs, self.inputs, 1)
            
            if not (readable or writable or exceptional):
                    if not self.state['receiving']:
                        self.handle()
                        continue     

            for s in readable:
                data = s.recv(8192)
                if data:
                    self.stompBuffer.appendData(data) 
                    if s not in self.outputs:
                        self.outputs.append(s)
                else:
                    self.log(('closing',client_address, 'after reading no data'))
                    if s in self.outputs:
                        self.outputs.remove(s)
                    self.inputs.remove(s)
                    s.close() 

            for s in writable:
                try:
                    next_msg = self.message_queues['out'].get_nowait()
                except Queue.Empty:
                    self.outputs.remove(s)
                    pass
                else:
                    #self.log('sending "%s" to %s' % (next_msg, s.getpeername()))
                    s._send(next_msg)

            for s in exceptional:
                self.log(('handling exceptional condition for',s.getpeername()))
                self.inputs.remove(s)
                if s in self.outputs:
                    self.outputs.remove(s)
                s.close()
                
    def handle(self):
        while True:
            resp = None
            msg = self.stompBuffer.getOneMessage()
            if msg:
                #self.log("Handling: %s"%(msg))
                resp = self.react(msg)
            else:
                break
            
    def provide(self):
        msg = self.stompBuffer.getOneMessage()
        if msg:
            resp = self.react(msg)
        return msg
                            
    def _send(self,cmd):
        self.message_queues['out'].put(cmd)
        if self.sock not in self.outputs:
            self.outputs.append(self.sock)   
             
    def send(self,dest, msg, transactionid=None, content_type='text/plain'):
        cmd = stomper.send(dest, msg, transactionid, content_type)  
        self._send(cmd)
                                  
    def connect(self):
        cmd = stomper.connect(self.username, self.password, self.server)
        self._send(cmd)
        
    def connected(self, msg):
        self.sessionId = msg['headers']['session']
        self.log("Stomp Socket connected: session id '%s'." % (self.sessionId))
        self.state['connected'] = True
        #HACK: The stomper state machine is not really a state machine; so once we are connected, just do the subscribe
        self.subscribe()
           
    def subscribe(self):  
        u = uuid.uuid1()    
        idx = str(u)
        cmd = stomper.subscribe(self.destination, idx=idx,ack='client') 
        self._send(cmd) 
        self.state['subscribed'] = True    
        self.state['receiving'] = True 
        self.log("Stomp Socket Subscribing to: %s"%(self.destination))
        
    def ack(self, msg):
        message_id = msg['headers']['message-id']
        subscription = msg['headers']['subscription']
        transaction_id = None
        if msg['headers'].has_key('transaction-id'):
            transaction_id = msg['headers']['transaction-id']
        #self.log("MSG Body: %s"%(msg['body']))
        #self.log("acknowledging message id <%s>." % (message_id))
        self._send(stomper.ack(message_id, subscription, transaction_id))  
        return msg
        
class Scrape(Interface):
    log = Logger()
    def start(self):
        try:
            self.queue = "/queue/firefly_work"
            self.server = "activemq.labs.g2-inc.net"
            self.stompBuffer = StompSocketBuffer()
            self.stompSocket = StompSocket(buffer=self.stompBuffer, log=self.log, destination=self.queue, server=self.server)  
            #INFO: run the socket in its own thread using the reactor
            reactor.callInThread(self.stompSocket.start)    
        except Exception as e:
            self.log("Stomp Init: %s"%(str(e)),"error")
    
    def _scrape(self, data):
        res = 0
        regex = re.compile(r"^(?P<dice>\d*)d(?P<sides>\d+)((?P<sign>[+\-*/])(?P<modifier>\d+)+)?")
        match = regex.search(data.cmd['parameters']).groupdict()
        j = {'category':match['category'],
             'url':match['url']
             }
        self.stompSocket.send(self.queue,json.dumps(j))
        return (("%s %s crawler submitted for %s")%(data.usernick, match['category'], match['url']),0)

    def bang_scrape(self, data):
        # !roll
        resp, err = self._scrape(data)
        data.conn.msg(data.channel, resp)

    def question_scrape(self, data):
        # ?roll
        '''
            Returns help for the roll plugin.
        '''

        helpLines = (
            'Scrape Help:',
            '    <!>scrape <category> <url>',
            'Example Scrape: ',
            '    !scrape news cnn.com',
            'Result:',
            '    news crawler submitted for cnn.com'
        )

        for line in helpLines:
            data.conn.msg(data.channel, line)