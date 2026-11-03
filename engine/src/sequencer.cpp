#include "trading/sequencer.hpp"
#include <vector>
namespace trading {
SequenceResult MoldSequencer::receive(std::span<const std::byte> b,
    const std::function<void(std::uint64_t,std::span<const std::byte>)>& apply) {
    if(b.size()<20)throw ProtocolError("MoldUDP64 header truncated");
    if(ended_)throw ProtocolError("packet after session end");
    std::string session;session.reserve(10);
    for(std::size_t i=0;i<10;++i)session.push_back(static_cast<char>(read_be(b,i,1)));
    if(session_.empty())session_=session;
    else if(session_!=session)throw ProtocolError("MoldUDP64 session changed; explicit rebuild required");
    const auto first=read_be(b,10,8);const auto count=read_be(b,18,2);
    if(first==0)throw ProtocolError("invalid zero sequence");
    if(count==0||count==65535){
        if(b.size()!=20)throw ProtocolError("control packet has payload");
        if(first>expected_)return {SequenceStatus::Gap,0,expected_};
        if(count==65535){ended_=true;return {SequenceStatus::SessionEnd,0,expected_};}
        return {SequenceStatus::Heartbeat,0,expected_};
    }
    if(first>UINT64_MAX-count)throw ProtocolError("sequence overflow");
    std::vector<std::span<const std::byte>> messages;messages.reserve(static_cast<std::size_t>(count));
    std::size_t offset=20;
    for(std::uint64_t i=0;i<count;++i){
        const auto len=read_be(b,offset,2);offset+=2;
        if(!len||offset>b.size()||len>b.size()-offset)throw ProtocolError("MoldUDP64 message truncated");
        messages.push_back(b.subspan(offset,static_cast<std::size_t>(len)));offset+=static_cast<std::size_t>(len);
    }
    if(offset!=b.size())throw ProtocolError("MoldUDP64 trailing bytes");
    if(first>expected_)return {SequenceStatus::Gap,0,expected_};
    std::uint64_t applied=0;
    for(std::uint64_t i=0;i<count;++i){
        const auto sequence=first+i;if(sequence<expected_)continue;
        apply(sequence,messages[static_cast<std::size_t>(i)]);++expected_;++applied;
    }
    return {applied?SequenceStatus::Applied:SequenceStatus::Duplicate,applied,expected_};
}
}
