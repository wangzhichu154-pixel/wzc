#include "trading/itch.hpp"
#include <algorithm>

namespace trading {
std::uint64_t read_be(std::span<const std::byte> bytes, std::size_t offset, std::size_t width) {
    if (width == 0 || width > 8 || offset > bytes.size() || width > bytes.size() - offset)
        throw ProtocolError("field outside message boundary");
    std::uint64_t value = 0;
    for (std::size_t i = 0; i < width; ++i)
        value = (value << 8) | std::to_integer<unsigned char>(bytes[offset+i]);
    return value;
}
static char byte(std::span<const std::byte> b, std::size_t at) {
    return static_cast<char>(read_be(b, at, 1));
}
static Side side(char value) {
    if (value == 'B') return Side::Buy;
    if (value == 'S') return Side::Sell;
    throw ProtocolError("invalid order side");
}
Event decode_itch(std::span<const std::byte> b) {
    if (b.size() < 11) throw ProtocolError("ITCH header truncated");
    Event e;
    e.type = byte(b, 0);
    std::size_t expected = 0;
    switch (e.type) {
    case 'S': expected=12; break; case 'R': expected=39; break;
    case 'H': expected=25; break; case 'Y': expected=20; break;
    case 'L': expected=26; break; case 'V': expected=35; break;
    case 'W': expected=12; break; case 'K': expected=28; break;
    case 'J': expected=35; break; case 'h': expected=21; break;
    case 'A': expected=36; break; case 'F': expected=40; break;
    case 'E': expected=31; break; case 'C': expected=36; break;
    case 'X': expected=23; break; case 'D': expected=19; break;
    case 'U': expected=35; break; case 'P': expected=44; break;
    case 'Q': expected=40; break; case 'B': expected=19; break;
    case 'I': expected=50; break; case 'N': expected=20; break;
    default: throw ProtocolError("unsupported ITCH message type");
    }
    if (b.size() != expected) throw ProtocolError("ITCH message length mismatch");
    e.locate = static_cast<std::uint16_t>(read_be(b, 1, 2));
    e.timestamp_ns = read_be(b, 5, 6);
    if (e.timestamp_ns >= 86'400'000'000'000ULL)
        throw ProtocolError("invalid nanoseconds since midnight");
    auto stock = [&](std::size_t at) {
        for (std::size_t i=0;i<8;++i) e.stock[i]=byte(b,at+i);
    };
    switch (e.type) {
    case 'A': case 'F': case 'P':
        e.reference=read_be(b,11,8); e.side=side(byte(b,19));
        e.quantity=read_be(b,20,4); stock(24); e.price=static_cast<Price>(read_be(b,32,4));
        if (e.type=='P') e.match=read_be(b,36,8);
        break;
    case 'E': case 'C':
        e.reference=read_be(b,11,8); e.quantity=read_be(b,19,4); e.match=read_be(b,23,8);
        if(e.type=='C') {
            e.status=byte(b,31);
            if(e.status!='Y' && e.status!='N') throw ProtocolError("invalid printable flag");
            e.printable=e.status=='Y'; e.price=static_cast<Price>(read_be(b,32,4));
        }
        break;
    case 'X': e.reference=read_be(b,11,8); e.quantity=read_be(b,19,4); break;
    case 'D': e.reference=read_be(b,11,8); break;
    case 'U':
        e.reference=read_be(b,11,8);e.new_reference=read_be(b,19,8);
        e.quantity=read_be(b,27,4);e.price=static_cast<Price>(read_be(b,31,4));break;
    case 'Q':
        e.quantity=read_be(b,11,8);stock(19);e.price=static_cast<Price>(read_be(b,27,4));
        e.match=read_be(b,31,8);e.status=byte(b,39);break;
    case 'B': e.match=read_be(b,11,8);break;
    case 'R': case 'H': case 'h': case 'Y': case 'K': case 'J': case 'I': case 'N':
        stock(11);
        if(e.type=='H') e.status=byte(b,19);
        if(e.type=='h') e.status=byte(b,20);
        if(e.type=='Y') e.status=byte(b,19);
        break;
    case 'S': case 'W':
        e.status=byte(b,11);
        if(e.type=='S'&&e.status!='O'&&e.status!='S'&&e.status!='Q'&&e.status!='M'&&e.status!='E'&&e.status!='C')
            throw ProtocolError("invalid system event");
        break;
    default: break;
    }
    if ((e.type=='A'||e.type=='F'||e.type=='U'||e.type=='P'||e.type=='Q') &&
        (e.quantity==0||e.price<=0)) throw ProtocolError("zero quantity or price");
    if ((e.type=='E'||e.type=='C'||e.type=='X') && e.quantity==0)
        throw ProtocolError("zero reduction quantity");
    if(e.type=='C'&&e.price<=0)throw ProtocolError("invalid execution price");
    return e;
}
std::string stock_name(const std::array<char,8>& stock) {
    std::string s(stock.begin(),stock.end());
    while(!s.empty() && (s.back()==' '||s.back()=='\0')) s.pop_back();
    for (const auto c:s) if (c<32||c>126) throw ProtocolError("invalid stock symbol");
    return s;
}
}
