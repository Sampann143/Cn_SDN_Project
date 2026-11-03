from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel


def build():
    net = Mininet(controller=None, switch=OVSSwitch, autoSetMacs=True)

    net.addController('c0', controller=RemoteController,
                      ip='127.0.0.1', port=6653)

    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')

    # added in this order so IPs come out as h1=10.0.0.1 ... h6=10.0.0.6
    h1 = net.addHost('h1')   # emergency server
    h2 = net.addHost('h2')   # subscriber
    h3 = net.addHost('h3')   # subscriber
    h4 = net.addHost('h4')   # subscriber
    h5 = net.addHost('h5')   # background traffic source
    h6 = net.addHost('h6')   # background traffic sink

    # server side on s1
    net.addLink(h1, s1)
    net.addLink(h5, s1)
    # subscriber side on s2
    for h in (h2, h3, h4, h6):
        net.addLink(h, s2)
    # the inter-switch link every flow must cross (made a bottleneck in D2)
    net.addLink(s1, s2)

    return net


if __name__ == '__main__':
    setLogLevel('info')
    net = build()
    net.start()
    CLI(net)
    net.stop()
